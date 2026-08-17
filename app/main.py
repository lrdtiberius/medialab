from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .catalog import CatalogItem, MediaCatalog
from .config import Settings, load_settings
from .db import Database
from .library import LibraryIndex
from .library_probe import LibraryProbeManager
from .media_probe import MediaProbe
from .processor import MediaProcessor, ProcessingError
from .scanner import InboxScanner
from .security import BasicAuthMiddleware
from .service import MediaService
from .tmdb import TMDbClient, TMDbError

PACKAGE_ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
STATUS_LABELS = {
    "waiting": "Wartet auf vollständige Datei",
    "pending": "Bereit",
    "matched": "Erkannt",
    "unresolved": "Manuelle Auswahl nötig",
    "processing": "Wird verarbeitet",
    "processed": "Verarbeitet",
    "error": "Fehler",
    "ignored": "Ignoriert",
}
LIBRARY_LABELS = {"movies": "Filme", "series": "Serien", "anime": "Animes"}
MEDIA_TYPE_LABELS = {"movie": "Film", "tv": "Serie"}
CATALOG_SORTS = {
    "title": "Titel A–Z",
    "year_desc": "Jahr absteigend",
    "modified_desc": "Zuletzt geändert",
    "rating_desc": "Bewertung",
}
CATALOG_QUALITIES = {
    "all": "Alle Metadatenstände",
    "missing_nfo": "Ohne Haupt-NFO",
    "missing_poster": "Ohne Poster",
}


def _int_or_none(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _filesize(value: int | None) -> str:
    if value is None:
        return "–"
    size = float(value)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _basename(value: str | None) -> str:
    return Path(value).name if value else ""


def _date(value: float | int | None) -> str:
    if not value:
        return "–"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%d.%m.%Y %H:%M")
    except (OSError, OverflowError, TypeError, ValueError):
        return "–"



def _duration(value: float | int | None) -> str:
    if value is None:
        return "–"
    try:
        seconds = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return "–"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"


def _catalog_haystack(item: CatalogItem) -> str:
    return " ".join(
        (
            item.title,
            item.original_title,
            str(item.year or ""),
            item.relative_path,
            " ".join(item.genres),
        )
    ).casefold()


def _catalog_url(params: dict[str, Any]) -> str:
    clean = {
        key: value
        for key, value in params.items()
        if value not in (None, "", "all") or key in {"library", "quality"}
    }
    return f"/library?{urlencode(clean)}" if clean else "/library"


def _tail_lines(path: Path, limit: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-limit:]


def create_app() -> FastAPI:
    settings = load_settings()
    settings.data_root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log_path = settings.data_root / "medialab.log"
    root_logger = logging.getLogger()
    if not any(getattr(handler, "baseFilename", None) == str(log_path) for handler in root_logger.handlers):
        file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        file_handler.setLevel(getattr(logging, settings.log_level, logging.INFO))
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root_logger.addHandler(file_handler)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_root.mkdir(parents=True, exist_ok=True)
        settings.staging_root.mkdir(parents=True, exist_ok=True)
        for path in (
            settings.inbox_root,
            settings.movie_root,
            settings.tv_root,
            settings.anime_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

        database = Database(settings.database_path)
        database.initialize()
        pruned_at_start = database.prune_missing_sources(settings.inbox_root)
        if pruned_at_start:
            LOGGER.info(
                "Removed %s stale inbox job(s) during startup", pruned_at_start
            )
        tmdb = TMDbClient(settings)
        library_index = LibraryIndex(settings)
        media_probe = MediaProbe(settings, database)
        LOGGER.info(
            "Starting %s %s (build %s); ffprobe_available=%s; media_root=%s",
            settings.app_name,
            settings.app_version,
            settings.build_id,
            media_probe.available,
            settings.media_root,
        )
        catalog = MediaCatalog(settings, database)
        processor = MediaProcessor(settings, tmdb, library_index, catalog=catalog)
        service = MediaService(settings, database, tmdb, library_index, processor, media_probe)
        technical_indexer = LibraryProbeManager(settings, media_probe, catalog)
        scanner = InboxScanner(settings, database, service)

        app.state.settings = settings
        app.state.database = database
        app.state.tmdb = tmdb
        app.state.service = service
        app.state.scanner = scanner
        app.state.catalog = catalog
        app.state.media_probe = media_probe
        app.state.technical_indexer = technical_indexer
        scanner_task = asyncio.create_task(scanner.run(), name="medialab-inbox-scanner")
        if settings.library_probe_auto_start and settings.ffprobe_enabled:
            await technical_indexer.start()
        try:
            yield
        finally:
            await technical_indexer.close()
            scanner.stop()
            try:
                await asyncio.wait_for(scanner_task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                scanner_task.cancel()
            await tmdb.close()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(BasicAuthMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
    templates.env.filters["filesize"] = _filesize
    templates.env.filters["basename"] = _basename
    templates.env.filters["localdate"] = _date
    templates.env.filters["duration"] = _duration

    def context(request: Request, **extra: Any) -> dict[str, Any]:
        base = {
            "request": request,
            "settings": settings,
            "status_labels": STATUS_LABELS,
            "library_labels": LIBRARY_LABELS,
            "media_type_labels": MEDIA_TYPE_LABELS,
        }
        base.update(extra)
        return base

    def database(request: Request) -> Database:
        return request.app.state.database

    def service(request: Request) -> MediaService:
        return request.app.state.service

    def catalog(request: Request) -> MediaCatalog:
        return request.app.state.catalog

    def technical_indexer(request: Request) -> LibraryProbeManager:
        return request.app.state.technical_indexer

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, status: str | None = None):
        db = database(request)
        await asyncio.to_thread(db.prune_missing_sources, settings.inbox_root)
        statuses = [status] if status in STATUS_LABELS else None
        jobs = db.list_jobs(statuses=statuses, limit=200)
        return templates.TemplateResponse(
            request,
            "index.html",
            context(
                request,
                jobs=jobs,
                counts=db.counts(),
                selected_status=status,
                notice=request.query_params.get("notice"),
            ),
        )

    @app.get("/library", response_class=HTMLResponse)
    async def library_overview(
        request: Request,
        q: str = "",
        library: str = "all",
        sort: str = "title",
        quality: str = "all",
        view: str = "grid",
        page: int = 1,
        page_size: int | None = None,
    ):
        q = q.strip()[:160]
        if library not in {"all", *LIBRARY_LABELS}:
            library = "all"
        if sort not in CATALOG_SORTS:
            sort = "title"
        if quality not in CATALOG_QUALITIES:
            quality = "all"
        if view not in {"grid", "list"}:
            view = "grid"
        allowed_page_sizes = {24, 48, 96, settings.library_page_size}
        effective_page_size = page_size or settings.library_page_size
        if effective_page_size not in allowed_page_sizes:
            effective_page_size = settings.library_page_size

        all_items = list(await asyncio.to_thread(catalog(request).items))
        counts = {"all": len(all_items), "movies": 0, "series": 0, "anime": 0}
        missing_nfo = 0
        missing_poster = 0
        for item in all_items:
            counts[item.library] += 1
            missing_nfo += int(not item.has_nfo)
            missing_poster += int(not item.has_poster)

        items = all_items
        if library != "all":
            items = [item for item in items if item.library == library]
        if quality == "missing_nfo":
            items = [item for item in items if not item.has_nfo]
        elif quality == "missing_poster":
            items = [item for item in items if not item.has_poster]
        if q:
            terms = [term for term in q.casefold().split() if term]
            items = [
                item
                for item in items
                if all(term in _catalog_haystack(item) for term in terms)
            ]

        if sort == "year_desc":
            items.sort(key=lambda item: (item.year or 0, item.title.casefold()), reverse=True)
        elif sort == "modified_desc":
            items.sort(key=lambda item: (item.modified_at, item.title.casefold()), reverse=True)
        elif sort == "rating_desc":
            items.sort(key=lambda item: (item.rating or -1.0, item.title.casefold()), reverse=True)
        else:
            items.sort(key=lambda item: (item.title.casefold(), item.year or 0, item.library))

        total = len(items)
        total_pages = max(1, math.ceil(total / effective_page_size))
        page = min(max(page, 1), total_pages)
        start = (page - 1) * effective_page_size
        page_items = items[start : start + effective_page_size]

        base_params = {
            "q": q,
            "library": library,
            "sort": sort,
            "quality": quality,
            "view": view,
            "page_size": effective_page_size,
        }
        first_page = max(1, page - 3)
        last_page = min(total_pages, page + 3)
        page_links = [
            (number, _catalog_url({**base_params, "page": number}))
            for number in range(first_page, last_page + 1)
        ]

        return templates.TemplateResponse(
            request,
            "library.html",
            context(
                request,
                items=page_items,
                counts=counts,
                missing_nfo=missing_nfo,
                missing_poster=missing_poster,
                total=total,
                page=page,
                total_pages=total_pages,
                page_links=page_links,
                previous_url=(
                    _catalog_url({**base_params, "page": page - 1}) if page > 1 else None
                ),
                next_url=(
                    _catalog_url({**base_params, "page": page + 1})
                    if page < total_pages
                    else None
                ),
                q=q,
                selected_library=library,
                selected_sort=sort,
                selected_quality=quality,
                selected_view=view,
                page_size=effective_page_size,
                page_size_choices=sorted(allowed_page_sizes),
                sort_labels=CATALOG_SORTS,
                quality_labels=CATALOG_QUALITIES,
                notice=request.query_params.get("notice"),
                catalog_error=catalog(request).last_error,
                technical_scan=technical_indexer(request).status(),
                probe_counts=database(request).media_probe_counts(),
                ffprobe_available=request.app.state.media_probe.available,
            ),
        )

    @app.post("/library/refresh")
    async def library_refresh(request: Request):
        items = await asyncio.to_thread(catalog(request).refresh, True)
        notice = f"Mediathek neu eingelesen: {len(items)} Titel gefunden."
        return RedirectResponse(url=f"/library?notice={quote(notice)}", status_code=303)

    @app.post("/library/analyze")
    async def library_analyze(request: Request, force: str = Form("false")):
        started = await technical_indexer(request).start(
            force=force.strip().lower() in {"1", "true", "yes", "on"}
        )
        notice = (
            "Technik-Analyse im Hintergrund gestartet. Die Seite zeigt den Fortschritt live an."
            if started
            else "Eine Technik-Analyse läuft bereits."
        )
        return RedirectResponse(url=f"/library?notice={quote(notice)}", status_code=303)

    @app.get("/library/technical-status")
    async def library_technical_status(request: Request):
        return JSONResponse(technical_indexer(request).status())

    @app.get("/library/errors", response_class=HTMLResponse)
    async def library_errors(request: Request):
        errors = database(request).list_media_probe_errors(limit=1000)
        return templates.TemplateResponse(
            request,
            "library_errors.html",
            context(
                request,
                errors=errors,
                technical_scan=technical_indexer(request).status(),
                notice=request.query_params.get("notice"),
            ),
        )

    @app.post("/library/errors/retry")
    async def library_errors_retry(request: Request):
        form = await request.form()
        selected = [Path(value) for value in form.getlist("path") if str(value).strip()]
        known = {row["file_path"] for row in database(request).list_media_probe_errors(limit=5000)}
        selected = [path for path in selected if str(path) in known]
        started = await technical_indexer(request).start_paths(selected, force=True)
        if not selected:
            notice = "Keine Fehler ausgewählt."
        elif started:
            notice = f"Erneute Analyse für {len(selected)} ausgewählte Datei(en) gestartet."
        else:
            notice = "Analyse konnte nicht gestartet werden oder läuft bereits."
        return RedirectResponse(url=f"/library/errors?notice={quote(notice)}", status_code=303)

    @app.get("/library/{item_id}/artwork/{kind}")
    async def library_artwork(request: Request, item_id: str, kind: str):
        if kind not in {"poster", "backdrop"}:
            raise HTTPException(status_code=404, detail="Artwork nicht gefunden")
        path = await asyncio.to_thread(catalog(request).artwork, item_id, kind)
        if path is None:
            raise HTTPException(status_code=404, detail="Artwork nicht gefunden")
        media_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.post("/library/{item_id}/analyze")
    async def library_item_analyze(request: Request, item_id: str, force: str = Form("false")):
        item = await asyncio.to_thread(catalog(request).get, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Medieneintrag nicht gefunden")
        started = await technical_indexer(request).start(
            item_path=item.path,
            force=force.strip().lower() in {"1", "true", "yes", "on"},
        )
        notice = (
            "Technik-Analyse für diesen Ordner gestartet."
            if started
            else "Eine Technik-Analyse läuft bereits."
        )
        return RedirectResponse(
            url=f"/library/{item_id}?notice={quote(notice)}", status_code=303
        )

    @app.get("/library/{item_id}", response_class=HTMLResponse)
    async def library_detail(request: Request, item_id: str):
        details = await asyncio.to_thread(catalog(request).details, item_id)
        if details is None:
            raise HTTPException(status_code=404, detail="Medieneintrag nicht gefunden")
        return templates.TemplateResponse(
            request,
            "library_item.html",
            context(
                request,
                details=details,
                notice=request.query_params.get("notice"),
                technical_scan=technical_indexer(request).status(),
                ffprobe_available=request.app.state.media_probe.available,
            ),
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: int):
        job = database(request).get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
        return templates.TemplateResponse(
            request,
            "job.html",
            context(
                request,
                job=job,
                results=None,
                error=request.query_params.get("error"),
                notice=request.query_params.get("notice"),
            ),
        )

    @app.post("/scan")
    async def scan_now(request: Request):
        scanner = request.app.state.scanner
        count = await scanner.scan_once()
        notice = f"Scan abgeschlossen: {count} bereit(e) Datei(en) geprüft."
        if scanner.last_pruned_count:
            notice += f" {scanner.last_pruned_count} veraltete(r) Eintrag/Einträge entfernt."
        return RedirectResponse(
            url=f"/?notice={quote(notice)}",
            status_code=303,
        )

    @app.post("/jobs/{job_id}/search", response_class=HTMLResponse)
    async def search_job(
        request: Request,
        job_id: int,
        query: str = Form(...),
        media_type: str = Form(...),
        library: str = Form(...),
        year: str = Form(""),
    ):
        job = database(request).get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
        try:
            results = await service(request).search_job(
                job_id,
                query=query,
                media_type=media_type,
                year=_int_or_none(year),
                library=library,
            )
            job = database(request).get_job(job_id) or job
            error = None if results else "Keine Treffer gefunden. Bitte den Suchbegriff ändern."
        except (ValueError, TMDbError) as exc:
            results = []
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "job.html",
            context(request, job=job, results=results, error=error, notice=None),
        )

    @app.post("/jobs/{job_id}/process")
    async def process_job(
        request: Request,
        job_id: int,
        tmdb_id: int = Form(...),
        media_type: str = Form(...),
        library: str = Form(...),
        season: str = Form(""),
        episode: str = Form(""),
        episode_end: str = Form(""),
    ):
        try:
            await service(request).process_selected(
                job_id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                library=library,
                season=_int_or_none(season),
                episode=_int_or_none(episode),
                episode_end=_int_or_none(episode_end),
            )
        except (ValueError, ProcessingError) as exc:
            return RedirectResponse(
                url=f"/jobs/{job_id}?error={quote(str(exc))}", status_code=303
            )
        return RedirectResponse(
            url=f"/jobs/{job_id}?notice={quote('Verarbeitung abgeschlossen.')}",
            status_code=303,
        )

    @app.post("/jobs/{job_id}/retry")
    async def retry_job(request: Request, job_id: int):
        try:
            service(request).retry(job_id)
            await service(request).analyze_job(job_id, force=True)
            await service(request).auto_match(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/ignore")
    async def ignore_job(request: Request, job_id: int):
        try:
            service(request).ignore(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.get("/log", response_class=HTMLResponse)
    async def log_view(request: Request):
        log_path = settings.data_root / "medialab.log"
        lines = await asyncio.to_thread(_tail_lines, log_path, settings.log_tail_lines)
        return templates.TemplateResponse(
            request,
            "log.html",
            context(request, log_lines=lines, log_path=str(log_path)),
        )

    @app.get("/about", response_class=HTMLResponse)
    async def about(request: Request):
        return templates.TemplateResponse(request, "about.html", context(request))

    @app.get("/health")
    async def health(request: Request):
        checks = {
            "media_root_exists": settings.media_root.exists(),
            "inbox_exists": settings.inbox_root.exists(),
            "media_root_readable": os.access(settings.media_root, os.R_OK),
            "inbox_writable": os.access(settings.inbox_root, os.W_OK),
            "data_writable": os.access(settings.data_root, os.W_OK),
            "tmdb_configured": settings.tmdb_configured,
            "ffprobe_available": request.app.state.media_probe.available,
        }
        healthy = all(
            value
            for key, value in checks.items()
            if key not in {"tmdb_configured", "ffprobe_available"}
        )
        payload = {
            "status": "ok" if healthy else "degraded",
            "version": settings.app_version,
            "build_id": settings.build_id,
            "dry_run": settings.dry_run,
            "features": {
                "library": True,
                "ffprobe": True,
                "stale_inbox_cleanup": True,
                "support_button": bool(settings.support_url),
                "copy_completion_guard": True,
                "probe_error_browser": True,
                "log_view": True,
            },
            "checks": checks,
        }
        return JSONResponse(payload, status_code=200 if healthy else 503)

    @app.get("/build-info")
    async def build_info():
        return JSONResponse(
            {
                "name": settings.app_name,
                "version": settings.app_version,
                "build_id": settings.build_id,
                "library_route": "/library",
                "ffprobe_enabled": settings.ffprobe_enabled,
                "author": settings.author_name,
                "support_url": settings.support_url,
                "probe_error_route": "/library/errors",
                "log_route": "/log",
            }
        )

    return app


app = create_app()

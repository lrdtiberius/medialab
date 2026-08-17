from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .library import ExistingMedia, LibraryIndex
from .matcher import RankedCandidate, is_confident_match, rank_candidates
from .media_probe import MediaProbe, MediaProbeError
from .parser import parse_media_filename
from .processor import MediaProcessor, ProcessingError
from .tmdb import TMDbClient, TMDbError

LOGGER = logging.getLogger(__name__)


class MediaService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        tmdb: TMDbClient,
        library_index: LibraryIndex,
        processor: MediaProcessor,
        media_probe: MediaProbe,
    ) -> None:
        self.settings = settings
        self.database = database
        self.tmdb = tmdb
        self.library_index = library_index
        self.processor = processor
        self.media_probe = media_probe
        self._active_jobs: set[int] = set()
        self._active_lock = asyncio.Lock()

    async def analyze_job(self, job_id: int, *, force: bool = False) -> dict[str, Any] | None:
        """Read real stream information without blocking the event loop.

        A failed probe never blocks title matching. The filename-derived tags
        remain available as a fallback and the error is shown in the detail UI.
        """

        if not self.settings.ffprobe_enabled:
            return None
        job = self.database.get_job(job_id)
        if not job:
            return None
        source = Path(job["source_path"])
        try:
            info = await asyncio.to_thread(self.media_probe.probe, source, force=force)
        except MediaProbeError as exc:
            current = dict(job.get("technical_info") or {})
            current["error"] = str(exc)
            self.database.update_job(job_id, technical_info=current)
            return None

        tags = list(info.get("technical_tags") or [])
        if not tags:
            tags = list(job.get("technical_tags") or [])
        self.database.update_job(job_id, technical_tags=tags, technical_info=info)
        return info

    def is_anime(self, item: dict[str, Any]) -> bool:
        if not self.settings.anime_auto_detect:
            return False
        genre_ids = set(item.get("genre_ids") or [])
        genre_ids.update(
            genre.get("id") for genre in (item.get("genres") or []) if genre.get("id") is not None
        )
        original_language = (item.get("original_language") or "").lower()
        return 16 in genre_ids and original_language in self.settings.anime_languages

    @staticmethod
    def validate_library(media_type: str, library: str) -> None:
        allowed = {
            "movie": {"movies", "anime"},
            "tv": {"series"},
        }
        if media_type not in allowed:
            raise ValueError("Ungültiger Medientyp.")
        if library not in allowed[media_type]:
            if media_type == "movie":
                raise ValueError("Filme dürfen nur nach Filme oder Animes verarbeitet werden.")
            raise ValueError("Serien und Folgen werden immer nach Serien verarbeitet.")

    def choose_library(
        self,
        media_type: str,
        item: dict[str, Any],
        source_hint: str | None,
    ) -> str:
        if media_type == "tv":
            # Feste Regel: Jede Episode landet in Serien, auch Anime-Episoden
            # und Dateien, die unter New/Animes abgelegt wurden.
            return "series"
        if media_type != "movie":
            raise ValueError("Ungültiger Medientyp.")
        if source_hint == "anime":
            return "anime"
        if source_hint == "movies":
            return "movies"
        return "anime" if self.is_anime(item) else "movies"

    async def _enter_job(self, job_id: int) -> bool:
        async with self._active_lock:
            if job_id in self._active_jobs:
                return False
            self._active_jobs.add(job_id)
            return True

    async def _leave_job(self, job_id: int) -> None:
        async with self._active_lock:
            self._active_jobs.discard(job_id)

    def _find_existing(self, job: dict[str, Any]) -> ExistingMedia | None:
        if job["media_type"] == "tv":
            preferred = "series"
        else:
            preferred = job.get("source_hint")
            if preferred not in {"movies", "anime"}:
                preferred = None
        return self.library_index.find_best(
            job["parsed_title"],
            job.get("parsed_year"),
            job["media_type"],
            preferred_library=preferred,
            threshold=0.96,
        )

    async def auto_match(self, job_id: int) -> None:
        if not await self._enter_job(job_id):
            return
        try:
            job = self.database.get_job(job_id)
            if not job or job["status"] not in {"pending", "matched"}:
                return
            if not self.settings.tmdb_configured:
                self.database.update_job(
                    job_id,
                    status="unresolved",
                    message=f"TMDB-Zugangsdaten fehlen. Bitte {self.settings.tmdb_credentials_file} prüfen.",
                )
                return

            if job["media_type"] == "tv" and (job.get("season") is None or job.get("episode") is None):
                self.database.update_job(
                    job_id,
                    status="unresolved",
                    message="Staffel oder Episode konnte nicht aus dem Dateinamen gelesen werden.",
                )
                return

            existing = self._find_existing(job)
            if existing and existing.tmdb_id:
                self.database.update_job(
                    job_id,
                    status="matched",
                    library=existing.library,
                    tmdb_id=existing.tmdb_id,
                    tmdb_title=existing.title,
                    tmdb_year=existing.year,
                    match_score=1.0,
                    message=f"Vorhandener Bibliotheksordner erkannt: {existing.path.name}",
                )
                if self.settings.auto_process:
                    await self._process_selected_unlocked(
                        job_id,
                        tmdb_id=existing.tmdb_id,
                        media_type=job["media_type"],
                        library=existing.library,
                        season=job.get("season"),
                        episode=job.get("episode"),
                        episode_end=job.get("episode_end"),
                    )
                return

            try:
                candidates = await self.tmdb.search(
                    job["media_type"],
                    job["parsed_title"],
                    job.get("parsed_year"),
                )
            except TMDbError as exc:
                self.database.update_job(job_id, status="error", message=str(exc))
                return

            ranked = rank_candidates(job["parsed_title"], job.get("parsed_year"), candidates)
            if not is_confident_match(
                ranked,
                self.settings.auto_match_threshold,
                self.settings.auto_match_margin,
                year_was_present=bool(job.get("parsed_year")),
            ):
                reason = "Keine passenden TMDB-Treffer gefunden." if not ranked else (
                    f"Treffer nicht eindeutig genug (bester Wert {ranked[0].score * 100:.0f} %)."
                )
                self.database.update_job(job_id, status="unresolved", message=reason)
                return

            top = ranked[0]
            candidate = top.candidate
            library = self.choose_library(
                job["media_type"], candidate, job.get("source_hint")
            )
            self.database.update_job(
                job_id,
                status="matched",
                library=library,
                tmdb_id=candidate["id"],
                tmdb_title=candidate["title"],
                tmdb_year=candidate.get("year"),
                match_score=top.score,
                message=f"Automatisch erkannt: {candidate['title']} ({candidate.get('year') or 'ohne Jahr'})",
            )
            if self.settings.auto_process:
                await self._process_selected_unlocked(
                    job_id,
                    tmdb_id=candidate["id"],
                    media_type=job["media_type"],
                    library=library,
                    season=job.get("season"),
                    episode=job.get("episode"),
                    episode_end=job.get("episode_end"),
                )
        except Exception:
            LOGGER.exception("Unexpected automatic matching error for job %s", job_id)
            self.database.update_job(
                job_id,
                status="error",
                message="Unerwarteter Fehler bei der automatischen Erkennung. Details stehen im Container-Log.",
            )
        finally:
            await self._leave_job(job_id)

    async def search_job(
        self,
        job_id: int,
        *,
        query: str,
        media_type: str,
        year: int | None,
        library: str,
    ) -> list[dict[str, Any]]:
        job = self.database.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        query = query.strip()
        if not query:
            raise ValueError("Bitte einen Suchbegriff eingeben.")
        self.validate_library(media_type, library)
        self.database.update_job(
            job_id,
            search_query=query,
            media_type=media_type,
            library=library,
            message="Manuelle TMDB-Suche ausgeführt.",
        )
        candidates = await self.tmdb.search(media_type, query, year)
        ranked = rank_candidates(query, year, candidates)[:20]
        results: list[dict[str, Any]] = []
        for ranked_item in ranked:
            candidate = dict(ranked_item.candidate)
            candidate["score"] = ranked_item.score
            candidate["score_percent"] = round(ranked_item.score * 100)
            candidate["is_anime"] = (
                candidate.get("media_type") == "movie" and self.is_anime(candidate)
            )
            candidate["poster_url"] = await self.tmdb.image_url(candidate.get("poster_path"), "w342")
            results.append(candidate)
        return results

    async def process_selected(
        self,
        job_id: int,
        *,
        tmdb_id: int,
        media_type: str,
        library: str,
        season: int | None,
        episode: int | None,
        episode_end: int | None,
    ) -> None:
        if not await self._enter_job(job_id):
            raise ProcessingError("Dieser Eintrag wird bereits verarbeitet.")
        try:
            await self._process_selected_unlocked(
                job_id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                library=library,
                season=season,
                episode=episode,
                episode_end=episode_end,
            )
        finally:
            await self._leave_job(job_id)

    async def _process_selected_unlocked(
        self,
        job_id: int,
        *,
        tmdb_id: int,
        media_type: str,
        library: str,
        season: int | None,
        episode: int | None,
        episode_end: int | None,
    ) -> None:
        job = self.database.get_job(job_id)
        if not job:
            raise ProcessingError("Eintrag wurde nicht gefunden.")
        try:
            self.validate_library(media_type, library)
        except ValueError as exc:
            raise ProcessingError(str(exc)) from exc
        self.database.update_job(
            job_id,
            status="processing",
            media_type=media_type,
            library=library,
            season=season,
            episode=episode,
            episode_end=episode_end,
            tmdb_id=tmdb_id,
            message="Datei wird verarbeitet …",
        )
        job = self.database.get_job(job_id) or job
        try:
            result = await self.processor.process(
                job,
                tmdb_id=tmdb_id,
                media_type=media_type,
                library=library,
                season=season,
                episode=episode,
                episode_end=episode_end,
            )
        except ProcessingError as exc:
            self.database.update_job(job_id, status="error", message=str(exc))
            raise
        except Exception as exc:
            LOGGER.exception("Unexpected processing error for job %s", job_id)
            self.database.update_job(
                job_id,
                status="error",
                message="Unerwarteter Verarbeitungsfehler. Details stehen im Container-Log.",
            )
            raise ProcessingError(str(exc)) from exc

        warning_text = f" Hinweise: {'; '.join(result.warnings)}" if result.warnings else ""
        if result.dry_run:
            self.database.update_job(
                job_id,
                status="matched",
                target_path=str(result.target_path),
                message=result.message + warning_text,
            )
        else:
            self.media_probe.relocate(Path(job["source_path"]), result.target_path)
            self.database.update_job(
                job_id,
                status="processed",
                target_path=str(result.target_path),
                tmdb_title=result.display_title,
                message=result.message + warning_text,
                processed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )

    def retry(self, job_id: int) -> None:
        job = self.database.get_job(job_id)
        if not job:
            raise KeyError(job_id)

        # Re-parse the original filename so parser improvements also apply to
        # entries that were already stored in the SQLite database.
        source_hint = job.get("source_hint")
        parsed = parse_media_filename(
            Path(job["source_path"]),
            anime_hint=source_hint in {"series", "anime"},
        )
        if source_hint == "movies":
            parsed.media_type = "movie"
            parsed.season = parsed.episode = parsed.episode_end = None
        elif source_hint == "series":
            parsed.media_type = "tv"

        if parsed.media_type == "tv":
            library = "series"
        elif source_hint in {"movies", "anime"}:
            library = source_hint
        else:
            library = None

        self.database.update_job(
            job_id,
            media_type=parsed.media_type,
            library=library,
            parsed_title=parsed.title,
            parsed_year=parsed.year,
            season=parsed.season,
            episode=parsed.episode,
            episode_end=parsed.episode_end,
            technical_tags=parsed.technical_tags,
            search_query=parsed.title,
        )
        self.database.reset_job(job_id)

    def ignore(self, job_id: int) -> None:
        if not self.database.get_job(job_id):
            raise KeyError(job_id)
        self.database.update_job(
            job_id,
            status="ignored",
            message="Eintrag wird ignoriert; die Datei bleibt im New-Ordner.",
        )

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog import MediaCatalog
from .config import Settings
from .media_probe import MediaProbe, ProbeError
from .parser import VIDEO_EXTENSIONS

LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LibraryProbeManager:
    """Background ffprobe analysis for the existing media collection."""

    def __init__(
        self,
        settings: Settings,
        media_probe: MediaProbe,
        catalog: MediaCatalog,
    ) -> None:
        self.settings = settings
        self.media_probe = media_probe
        self.catalog = catalog
        self._task: asyncio.Task[None] | None = None
        self._cancel = asyncio.Event()
        self._state: dict[str, Any] = {
            "running": False,
            "scope": "",
            "total": 0,
            "completed": 0,
            "probed": 0,
            "cached": 0,
            "errors": 0,
            "current": "",
            "started_at": "",
            "finished_at": "",
            "message": "Noch keine technische Analyse gestartet.",
            "recent_errors": [],
        }

    @staticmethod
    def _video_paths(roots: tuple[Path, ...]) -> list[Path]:
        paths: list[Path] = []
        for root in roots:
            try:
                exists = root.exists()
            except OSError:
                exists = False
            if not exists:
                continue
            try:
                candidates = root.rglob("*")
                for path in candidates:
                    try:
                        relative = path.relative_to(root)
                    except ValueError:
                        continue
                    if any(part.startswith(".") or part.startswith("_") for part in relative.parts[:-1]):
                        continue
                    try:
                        is_file = path.is_file()
                    except OSError:
                        continue
                    if is_file and path.suffix.lower() in VIDEO_EXTENSIONS:
                        paths.append(path.resolve())
            except OSError as exc:
                LOGGER.warning("Could not enumerate media files below %s: %s", root, exc)
        return sorted(set(paths), key=lambda value: str(value).casefold())

    def status(self) -> dict[str, Any]:
        state = dict(self._state)
        total = int(state.get("total") or 0)
        completed = int(state.get("completed") or 0)
        state["percent"] = round((completed / total) * 100, 1) if total else 0.0
        state["ffprobe_enabled"] = self.settings.ffprobe_enabled
        # UI aliases retained for the live status widget.
        state["analyzed"] = int(state.get("probed") or 0)
        state["current_file"] = str(state.get("current") or "")
        return state

    async def start(
        self,
        *,
        item_id: str | None = None,
        item_path: Path | None = None,
        force: bool = False,
    ) -> bool:
        if not self.settings.ffprobe_enabled:
            self._state["message"] = "FFprobe ist deaktiviert."
            return False
        if not self.media_probe.available:
            self._state["message"] = "FFprobe ist im Container nicht verfügbar."
            return False
        if self._task and not self._task.done():
            return False

        if item_path is not None:
            roots = (Path(item_path).resolve(),)
            scope = Path(item_path).name
        elif item_id:
            item = await asyncio.to_thread(self.catalog.get, item_id)
            if item is None:
                raise KeyError(item_id)
            roots = (item.path,)
            scope = item.title
        else:
            roots = (
                self.settings.movie_root,
                self.settings.tv_root,
                self.settings.anime_root,
            )
            scope = "gesamte Mediathek"

        self._cancel = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(roots=roots, scope=scope, force=force),
            name="medialab-library-ffprobe",
        )
        return True

    async def start_paths(self, paths: list[Path], *, force: bool = True) -> bool:
        if not self.settings.ffprobe_enabled or not self.media_probe.available:
            self._state["message"] = "FFprobe ist nicht verfügbar."
            return False
        if self._task and not self._task.done():
            return False
        allowed_roots = tuple(path.resolve() for path in (
            self.settings.movie_root, self.settings.tv_root, self.settings.anime_root
        ))
        safe_paths: list[Path] = []
        for candidate in paths:
            resolved = Path(candidate).resolve()
            if any(root == resolved or root in resolved.parents for root in allowed_roots):
                safe_paths.append(resolved)
        if not safe_paths:
            return False
        self._cancel = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(roots=(), scope=f"{len(safe_paths)} ausgewählte Fehler", force=force, explicit_paths=safe_paths),
            name="medialab-library-ffprobe-selected",
        )
        return True

    async def cancel(self) -> None:
        self._cancel.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()

    async def close(self) -> None:
        await self.cancel()

    async def _run(self, *, roots: tuple[Path, ...], scope: str, force: bool, explicit_paths: list[Path] | None = None) -> None:
        self._state = {
            "running": True,
            "scope": scope,
            "total": 0,
            "completed": 0,
            "probed": 0,
            "cached": 0,
            "errors": 0,
            "current": "",
            "started_at": _now(),
            "finished_at": "",
            "message": "Videodateien werden gesucht …",
            "recent_errors": [],
        }
        try:
            paths = list(explicit_paths) if explicit_paths is not None else await asyncio.to_thread(self._video_paths, roots)
            self._state["total"] = len(paths)
            if not paths:
                self._state["message"] = "Keine Videodateien gefunden."
                return

            queue: asyncio.Queue[Path] = asyncio.Queue()
            for path in paths:
                queue.put_nowait(path)

            async def worker() -> None:
                while not self._cancel.is_set():
                    try:
                        path = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    self._state["current"] = str(path)
                    try:
                        cached = None if force else await asyncio.to_thread(self.media_probe.cached, path)
                        if cached:
                            self._state["cached"] += 1
                        else:
                            await asyncio.to_thread(self.media_probe.probe, path, force=force)
                            self._state["probed"] += 1
                    except ProbeError as exc:
                        self._state["errors"] += 1
                        recent = self._state.setdefault("recent_errors", [])
                        recent.append({"path": str(path), "error": str(exc)})
                        del recent[:-20]
                        LOGGER.warning("ffprobe failed for %s: %s", path, exc)
                    except Exception as exc:
                        self._state["errors"] += 1
                        recent = self._state.setdefault("recent_errors", [])
                        recent.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
                        del recent[:-20]
                        LOGGER.exception("Unexpected library probe error for %s", path)
                    finally:
                        self._state["completed"] += 1
                        self._state["message"] = (
                            f"{self._state['completed']} von {self._state['total']} Dateien analysiert."
                        )
                        queue.task_done()

            worker_count = min(
                max(1, self.settings.library_probe_concurrency),
                max(1, len(paths)),
            )
            workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
            await asyncio.gather(*workers)
            if self._cancel.is_set():
                self._state["message"] = "Technische Analyse wurde abgebrochen."
            else:
                self._state["message"] = (
                    f"Technische Analyse abgeschlossen: {self._state['probed']} neu, "
                    f"{self._state['cached']} aus Cache, {self._state['errors']} Fehler."
                )
        finally:
            self._state["running"] = False
            self._state["current"] = ""
            self._state["finished_at"] = _now()
            self.catalog.invalidate()

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .parser import is_video_file, parse_media_filename
from .service import MediaService

LOGGER = logging.getLogger(__name__)

_HINTS = {
    "filme": "movies",
    "film": "movies",
    "movies": "movies",
    "movie": "movies",
    "serien": "series",
    "serie": "series",
    "series": "series",
    "tv": "series",
    "animes": "anime",
    "anime": "anime",
}


class InboxScanner:
    def __init__(self, settings: Settings, database: Database, service: MediaService):
        self.settings = settings
        self.database = database
        self.service = service
        self._scan_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self.last_pruned_count = 0
        self._stability: dict[str, tuple[int, float, int]] = {}

    def stop(self) -> None:
        self._stop.set()

    def _source_hint(self, path: Path) -> str | None:
        try:
            relative = path.relative_to(self.settings.inbox_root)
        except ValueError:
            return None
        if len(relative.parts) < 2:
            return None
        return _HINTS.get(relative.parts[0].casefold())

    @staticmethod
    def _seconds_since(value: str) -> float:
        try:
            timestamp = datetime.fromisoformat(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return (datetime.now(UTC) - timestamp).total_seconds()
        except (TypeError, ValueError):
            return 0.0

    def _stable_checks(self, path: Path, size_bytes: int, mtime: float) -> int:
        key = str(path)
        previous = self._stability.get(key)
        if previous and previous[0] == size_bytes and abs(previous[1] - mtime) < 0.0001:
            checks = previous[2] + 1
        else:
            checks = 1
        self._stability[key] = (size_bytes, mtime, checks)
        return checks

    @staticmethod
    def _readable_at_edges(path: Path, size_bytes: int) -> bool:
        """Cheap final sanity check without running ffprobe on a growing copy."""
        if size_bytes <= 0:
            return False
        try:
            with path.open("rb", buffering=0) as handle:
                if not handle.read(1):
                    return False
                if size_bytes > 1:
                    handle.seek(-1, 2)
                    if not handle.read(1):
                        return False
            return True
        except OSError:
            return False

    def _is_copy_complete(self, path: Path, stat: Any, job: dict[str, Any], checks: int) -> bool:
        if checks < self.settings.file_stable_min_checks:
            return False
        if self._seconds_since(job["stable_since"]) < self.settings.file_stable_seconds:
            return False
        if (time.time() - float(stat.st_mtime)) < self.settings.file_stable_mtime_seconds:
            return False
        return self._readable_at_edges(path, int(stat.st_size))

    def _discover_sync(self) -> list[dict[str, Any]]:
        self.settings.inbox_root.mkdir(parents=True, exist_ok=True)
        ready: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in self.settings.inbox_root.rglob("*"):
            try:
                relative = path.relative_to(self.settings.inbox_root)
            except ValueError:
                continue
            if any(part.startswith(".") or part.startswith("_") for part in relative.parts[:-1]):
                continue
            if not is_video_file(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            resolved_path = path.resolve()
            key = str(resolved_path)
            seen.add(key)
            checks = self._stable_checks(resolved_path, stat.st_size, stat.st_mtime)
            hint = self._source_hint(path)
            parsed = parse_media_filename(path, anime_hint=hint in {"series", "anime"})
            if hint == "movies":
                parsed.media_type = "movie"
                parsed.season = parsed.episode = parsed.episode_end = None
            elif hint == "series":
                parsed.media_type = "tv"

            effective_hint = "series" if parsed.media_type == "tv" else hint
            job = self.database.upsert_discovered(
                resolved_path, stat.st_size, stat.st_mtime, parsed, effective_hint
            )
            if job["status"] == "waiting" and self._is_copy_complete(path, stat, job, checks):
                self.database.mark_ready(job["id"])
                LOGGER.info(
                    "Inbox file is stable and complete after %s checks: %s (%s bytes)",
                    checks,
                    resolved_path,
                    stat.st_size,
                )
                job = self.database.get_job(job["id"]) or job
            elif job["status"] == "waiting":
                LOGGER.debug(
                    "Waiting for copy to settle: %s (stable_checks=%s/%s)",
                    resolved_path,
                    checks,
                    self.settings.file_stable_min_checks,
                )
            if job["status"] == "pending":
                ready.append(job)

        for key in list(self._stability):
            if key not in seen:
                self._stability.pop(key, None)

        pruned = self.database.prune_missing_sources(self.settings.inbox_root)
        self.last_pruned_count = pruned
        if pruned:
            LOGGER.info("Removed %s stale inbox job(s) after rename/delete", pruned)
        return ready

    async def scan_once(self) -> int:
        async with self._scan_lock:
            ready = await asyncio.to_thread(self._discover_sync)
            for job in ready:
                if self.settings.ffprobe_enabled:
                    await self.service.analyze_job(job["id"])
                await self.service.auto_match(job["id"])
            self.database.trim_history(self.settings.history_limit)
            return len(ready)

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Inbox scan failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.scan_interval_seconds
                )
            except TimeoutError:
                pass

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .artwork import ArtworkManager
from .catalog import MediaCatalog
from .config import Settings
from .library import LibraryIndex
from .nfo import write_episode_nfo, write_movie_nfo, write_tvshow_nfo
from .parser import SUBTITLE_EXTENSIONS, sanitize_component
from .tmdb import TMDbClient, TMDbError

LOGGER = logging.getLogger(__name__)


class ProcessingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessResult:
    target_path: Path
    display_title: str
    message: str
    warnings: tuple[str, ...] = ()
    dry_run: bool = False


class MediaProcessor:
    def __init__(
        self,
        settings: Settings,
        tmdb: TMDbClient,
        library_index: LibraryIndex,
        catalog: MediaCatalog | None = None,
    ) -> None:
        self.settings = settings
        self.tmdb = tmdb
        self.library_index = library_index
        self.catalog = catalog
        self.artwork = ArtworkManager(settings, tmdb)

    def _invalidate_library_caches(self) -> None:
        self.library_index.invalidate()
        if self.catalog is not None:
            self.catalog.invalidate()

    @staticmethod
    def _year(date_value: str | None) -> int | None:
        if not date_value or len(date_value) < 4:
            return None
        try:
            return int(date_value[:4])
        except ValueError:
            return None

    @staticmethod
    def _folder_name(title: str, year: int | None) -> str:
        title = sanitize_component(title)
        return f"{title} ({year})" if year else title

    @staticmethod
    def _tags(job: dict[str, Any], enabled: bool) -> str:
        tags = job.get("technical_tags") or []
        return f" {' '.join(tags)}" if enabled and tags else ""

    def _validate_source(self, job: dict[str, Any]) -> Path:
        source = Path(job["source_path"]).resolve()
        try:
            source.relative_to(self.settings.inbox_root)
        except ValueError as exc:
            raise ProcessingError("Die Quelldatei liegt nicht im New-Ordner.") from exc
        if not source.exists() or not source.is_file():
            raise ProcessingError("Die Quelldatei wurde nicht mehr gefunden.")
        return source

    def _existing_folder(
        self,
        tmdb_id: int,
        title: str,
        year: int | None,
        media_type: str,
        library: str,
    ) -> Path | None:
        item = self.library_index.find_by_tmdb(tmdb_id, media_type, library)
        if item:
            return item.path
        item = self.library_index.find_best(
            title,
            year,
            media_type,
            preferred_library=library,
            threshold=0.97,
        )
        return item.path if item else None

    async def process(
        self,
        job: dict[str, Any],
        *,
        tmdb_id: int,
        media_type: str,
        library: str,
        season: int | None = None,
        episode: int | None = None,
        episode_end: int | None = None,
    ) -> ProcessResult:
        source = self._validate_source(job)
        if library not in self.settings.libraries:
            raise ProcessingError("Unbekannte Zielbibliothek.")
        if media_type == "movie":
            return await self._process_movie(job, source, tmdb_id, library)
        if media_type == "tv":
            if season is None or episode is None:
                raise ProcessingError("Für Serien werden Staffel und Episode benötigt.")
            return await self._process_episode(
                job,
                source,
                tmdb_id,
                library,
                int(season),
                int(episode),
                int(episode_end) if episode_end is not None else None,
            )
        raise ProcessingError("Unbekannter Medientyp.")

    async def _process_movie(
        self,
        job: dict[str, Any],
        source: Path,
        tmdb_id: int,
        library: str,
    ) -> ProcessResult:
        try:
            details = await self.tmdb.movie_details(tmdb_id)
        except TMDbError as exc:
            raise ProcessingError(str(exc)) from exc
        title = details.get("title") or details.get("original_title") or job["parsed_title"]
        year = self._year(details.get("release_date"))
        library_root = self.settings.library_path(library)
        existing = self._existing_folder(tmdb_id, title, year, "movie", library)
        target_dir = existing or (library_root / self._folder_name(title, year))
        base_name = self._folder_name(title, year) + self._tags(job, self.settings.keep_technical_tags)
        target_video = target_dir / f"{sanitize_component(base_name)}{source.suffix.lower()}"

        self._assert_target_available(target_video)
        if self.settings.dry_run:
            return ProcessResult(
                target_path=target_video,
                display_title=self._folder_name(title, year),
                message=f"Simulation: würde nach {target_video} verschieben.",
                dry_run=True,
            )

        stage = self._new_stage(job["id"])
        warnings: list[str] = []
        try:
            if self.settings.create_nfo:
                await asyncio.to_thread(
                    write_movie_nfo,
                    details,
                    stage / "movie.nfo",
                    self.settings.tmdb_region,
                )
                if self.settings.duplicate_movie_nfo:
                    await asyncio.to_thread(
                        write_movie_nfo,
                        details,
                        stage / f"{sanitize_component(base_name)}.nfo",
                        self.settings.tmdb_region,
                    )
            warnings.extend(await self.artwork.movie(details, stage))
            await asyncio.to_thread(self._install_tree, stage, target_dir)
            await asyncio.to_thread(self._move_with_sidecars, source, target_video)
            await asyncio.to_thread(self._cleanup_empty_inbox_parents, source.parent)
        finally:
            await asyncio.to_thread(shutil.rmtree, stage, True)

        self._invalidate_library_caches()
        message = f"Film verarbeitet: {target_video.name}"
        if warnings:
            message += " (mit Hinweisen)"
        return ProcessResult(
            target_path=target_video,
            display_title=self._folder_name(title, year),
            message=message,
            warnings=tuple(warnings),
        )

    async def _process_episode(
        self,
        job: dict[str, Any],
        source: Path,
        tmdb_id: int,
        library: str,
        season: int,
        episode: int,
        episode_end: int | None,
    ) -> ProcessResult:
        try:
            series = await self.tmdb.tv_details(tmdb_id)
            season_info = await self.tmdb.season_details(tmdb_id, season)
            numbers = list(range(episode, episode_end + 1)) if episode_end and episode_end >= episode else [episode]
            episodes = [await self.tmdb.episode_details(tmdb_id, season, number) for number in numbers]
        except TMDbError as exc:
            raise ProcessingError(str(exc)) from exc

        series_title = series.get("name") or series.get("original_name") or job["parsed_title"]
        year = self._year(series.get("first_air_date"))
        library_root = self.settings.library_path(library)
        existing = self._existing_folder(tmdb_id, series_title, year, "tv", library)
        series_dir = existing or (library_root / self._folder_name(series_title, year))
        season_dir = series_dir / f"Season {season}"

        episode_names = [
            sanitize_component(item.get("name") or f"Episode {item.get('episode_number', number)}")
            for item, number in zip(episodes, numbers, strict=True)
        ]
        episode_title = " + ".join(episode_names)
        code = f"S{season:02d}E{episode:02d}"
        if episode_end and episode_end > episode:
            code += f"-E{episode_end:02d}"
        base_name = f"{sanitize_component(series_title)} - {code} - {episode_title}"
        base_name += self._tags(job, self.settings.tv_keep_technical_tags)
        base_name = sanitize_component(base_name)
        target_video = season_dir / f"{base_name}{source.suffix.lower()}"

        self._assert_target_available(target_video)
        if self.settings.dry_run:
            return ProcessResult(
                target_path=target_video,
                display_title=f"{self._folder_name(series_title, year)} – {code}",
                message=f"Simulation: würde nach {target_video} verschieben.",
                dry_run=True,
            )

        stage = self._new_stage(job["id"])
        warnings: list[str] = []
        try:
            stage_season = stage / f"Season {season}"
            if self.settings.create_nfo:
                await asyncio.to_thread(
                    write_tvshow_nfo,
                    series,
                    stage / "tvshow.nfo",
                    self.settings.tmdb_region,
                )
                await asyncio.to_thread(
                    write_episode_nfo,
                    series,
                    episodes,
                    stage_season / f"{base_name}.nfo",
                )
            warnings.extend(await self.artwork.tv(series, season_info, stage, season))
            thumb_ok = await self.artwork.episode_thumb(
                episodes[0].get("still_path"),
                stage_season / f"{base_name}-thumb.jpg",
            )
            if self.settings.download_artwork and not thumb_ok:
                warnings.append("Kein Episodenbild verfügbar")
            await asyncio.to_thread(self._install_tree, stage, series_dir)
            await asyncio.to_thread(self._move_with_sidecars, source, target_video)
            await asyncio.to_thread(self._cleanup_empty_inbox_parents, source.parent)
        finally:
            await asyncio.to_thread(shutil.rmtree, stage, True)

        self._invalidate_library_caches()
        message = f"Episode verarbeitet: {target_video.name}"
        if warnings:
            message += " (mit Hinweisen)"
        return ProcessResult(
            target_path=target_video,
            display_title=f"{self._folder_name(series_title, year)} – {code}",
            message=message,
            warnings=tuple(warnings),
        )

    def _new_stage(self, job_id: int) -> Path:
        self.settings.staging_root.mkdir(parents=True, exist_ok=True)
        path = self.settings.staging_root / f"job-{job_id}-{uuid.uuid4().hex}"
        path.mkdir(parents=True)
        return path

    def _assert_target_available(self, target: Path) -> None:
        if target.exists() and not self.settings.overwrite_existing:
            raise ProcessingError(f"Zieldatei existiert bereits: {target}")

    def _install_tree(self, source_root: Path, destination_root: Path) -> None:
        destination_root.mkdir(parents=True, exist_ok=True)
        for source in sorted(source_root.rglob("*")):
            relative = source.relative_to(source_root)
            destination = destination_root / relative
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and not self.settings.overwrite_metadata:
                continue
            temp_destination = destination.with_name(f".{destination.name}.medialab.tmp")
            shutil.copy2(source, temp_destination)
            temp_destination.replace(destination)

    def _move_with_sidecars(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not self.settings.overwrite_existing:
                raise ProcessingError(f"Zieldatei existiert bereits: {target}")
            target.unlink()

        sidecars: list[tuple[Path, Path]] = []
        try:
            siblings = list(source.parent.iterdir())
        except OSError:
            siblings = []
        for candidate in siblings:
            if not candidate.is_file() or candidate == source:
                continue
            if candidate.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            if not candidate.name.startswith(source.stem):
                continue
            suffix = candidate.name[len(source.stem) :]
            sidecars.append((candidate, target.with_name(f"{target.stem}{suffix}")))

        shutil.move(str(source), str(target))
        for source_sidecar, target_sidecar in sidecars:
            if target_sidecar.exists() and not self.settings.overwrite_existing:
                LOGGER.warning("Subtitle target already exists, leaving source in New: %s", target_sidecar)
                continue
            if target_sidecar.exists():
                target_sidecar.unlink()
            shutil.move(str(source_sidecar), str(target_sidecar))

    def _cleanup_empty_inbox_parents(self, start: Path) -> None:
        current = start
        while current != self.settings.inbox_root:
            # Keep direct category folders such as New/Filme, New/Serien and New/Animes.
            if current.parent == self.settings.inbox_root:
                break
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

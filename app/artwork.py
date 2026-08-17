from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from .config import Settings
from .tmdb import TMDbClient, pick_image

LOGGER = logging.getLogger(__name__)


class ArtworkManager:
    def __init__(self, settings: Settings, tmdb: TMDbClient):
        self.settings = settings
        self.tmdb = tmdb
        primary = settings.tmdb_language.split("-")[0]
        fallback = settings.tmdb_fallback_language.split("-")[0]
        language_order: list[str | None] = []
        for value in (primary, fallback, None):
            if value not in language_order:
                language_order.append(value)
        self.language_order = tuple(language_order)

    async def _download(
        self,
        file_path: str | None,
        destination: Path,
        *,
        size: str,
        force_png: bool = False,
    ) -> bool:
        if destination.exists() and not self.settings.overwrite_metadata:
            return True
        return await self.tmdb.download_image(
            file_path,
            destination,
            size=size,
            force_png=force_png,
        )

    @staticmethod
    def _copy_if_missing(source: Path, destination: Path) -> None:
        if not source.exists() or destination.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    async def movie(self, details: dict[str, Any], destination: Path) -> list[str]:
        if not self.settings.download_artwork:
            return []
        warnings: list[str] = []
        images = details.get("images") or {}
        poster = pick_image(images.get("posters"), self.language_order) or details.get("poster_path")
        backdrop = pick_image(images.get("backdrops"), (None, *self.language_order)) or details.get("backdrop_path")
        logo = pick_image(images.get("logos"), self.language_order)

        if not await self._download(poster, destination / "poster.jpg", size="w780"):
            warnings.append("Kein Poster verfügbar")
        if not await self._download(backdrop, destination / "backdrop.jpg", size="w1280"):
            warnings.append("Kein Hintergrundbild verfügbar")
        if logo:
            await self._download(logo, destination / "logo.png", size="original", force_png=True)

        if self.settings.create_derived_artwork:
            self._copy_if_missing(destination / "backdrop.jpg", destination / "banner.jpg")
            self._copy_if_missing(destination / "backdrop.jpg", destination / "landscape.jpg")
        return warnings

    async def tv(
        self,
        details: dict[str, Any],
        season_details: dict[str, Any],
        series_destination: Path,
        season_number: int,
    ) -> list[str]:
        if not self.settings.download_artwork:
            return []
        warnings: list[str] = []
        images = details.get("images") or {}
        poster = pick_image(images.get("posters"), self.language_order) or details.get("poster_path")
        backdrop = pick_image(images.get("backdrops"), (None, *self.language_order)) or details.get("backdrop_path")
        logo = pick_image(images.get("logos"), self.language_order)

        if not await self._download(poster, series_destination / "poster.jpg", size="w780"):
            warnings.append("Kein Serienposter verfügbar")
        if not await self._download(backdrop, series_destination / "fanart.jpg", size="w1280"):
            warnings.append("Kein Serienhintergrund verfügbar")
        if logo:
            await self._download(logo, series_destination / "clearlogo.png", size="original", force_png=True)

        season_poster = season_details.get("poster_path")
        if season_poster:
            await self._download(
                season_poster,
                series_destination / f"season{season_number:02d}-poster.jpg",
                size="w780",
            )

        if self.settings.create_derived_artwork:
            self._copy_if_missing(series_destination / "fanart.jpg", series_destination / "banner.jpg")
            self._copy_if_missing(series_destination / "fanart.jpg", series_destination / "thumb.jpg")
            self._copy_if_missing(series_destination / "poster.jpg", series_destination / "keyart.jpg")
        return warnings

    async def episode_thumb(self, still_path: str | None, destination: Path) -> bool:
        if not self.settings.download_artwork or not still_path:
            return False
        return await self._download(still_path, destination, size="w780")

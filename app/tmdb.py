from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from .config import Settings

LOGGER = logging.getLogger(__name__)


class TMDbError(RuntimeError):
    pass


class TMDbClient:
    API_BASE = "https://api.themoviedb.org/3"

    def __init__(self, settings: Settings):
        self.settings = settings
        headers = {
            "Accept": "application/json",
            "User-Agent": f"Media-Ingest/{settings.app_version}",
        }
        if settings.tmdb_read_token:
            headers["Authorization"] = f"Bearer {settings.tmdb_read_token}"
        self.client = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0))
        self._configuration: dict[str, Any] | None = None
        self._configuration_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        if not self.settings.tmdb_configured:
            raise TMDbError("TMDB ist nicht konfiguriert. Bitte API-Token oder API-Key setzen.")
        if self.settings.tmdb_api_key and not self.settings.tmdb_read_token:
            params["api_key"] = self.settings.tmdb_api_key

        url = f"{self.API_BASE}{path}"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.client.get(url, params=params)
                if response.status_code == 429:
                    retry_after = min(float(response.headers.get("Retry-After", "1")), 10.0)
                    await asyncio.sleep(retry_after)
                    continue
                if response.status_code >= 500:
                    last_error = TMDbError(f"TMDB antwortete mit HTTP {response.status_code}")
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                if response.status_code == 404:
                    raise TMDbError("Der gewählte TMDB-Eintrag wurde nicht gefunden.")
                if response.status_code in {401, 403}:
                    raise TMDbError("TMDB-Zugangsdaten wurden abgelehnt.")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TMDbError("Unerwartete Antwort von TMDB.")
                return payload
            except TMDbError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
        raise TMDbError(f"TMDB ist momentan nicht erreichbar: {last_error}")

    async def configuration(self) -> dict[str, Any]:
        if self._configuration is not None:
            return self._configuration
        async with self._configuration_lock:
            if self._configuration is None:
                self._configuration = await self._get("/configuration")
        return self._configuration

    @staticmethod
    def _year_from_date(value: str | None) -> int | None:
        if not value or len(value) < 4:
            return None
        try:
            return int(value[:4])
        except ValueError:
            return None

    @staticmethod
    def _merge_missing(primary: dict[str, Any], fallback: dict[str, Any], keys: tuple[str, ...]) -> None:
        for key in keys:
            if not primary.get(key) and fallback.get(key):
                primary[key] = fallback[key]

    def _normalize_search_result(self, item: dict[str, Any], media_type: str) -> dict[str, Any]:
        if media_type == "movie":
            title = item.get("title") or item.get("original_title") or "Unbekannter Film"
            original_title = item.get("original_title")
            date = item.get("release_date")
        else:
            title = item.get("name") or item.get("original_name") or "Unbekannte Serie"
            original_title = item.get("original_name")
            date = item.get("first_air_date")
        return {
            "id": int(item["id"]),
            "media_type": media_type,
            "title": title,
            "original_title": original_title,
            "date": date,
            "year": self._year_from_date(date),
            "overview": item.get("overview") or "",
            "poster_path": item.get("poster_path"),
            "backdrop_path": item.get("backdrop_path"),
            "original_language": item.get("original_language") or "",
            "genre_ids": item.get("genre_ids") or [],
            "popularity": item.get("popularity") or 0.0,
            "vote_average": item.get("vote_average") or 0.0,
        }

    async def search_movie(self, query: str, year: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "query": query,
            "language": self.settings.tmdb_language,
            "region": self.settings.tmdb_region,
            "include_adult": "false",
            "page": 1,
        }
        if year:
            params["year"] = year
        payload = await self._get("/search/movie", **params)
        return [self._normalize_search_result(item, "movie") for item in payload.get("results", [])]

    async def search_tv(self, query: str, year: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "query": query,
            "language": self.settings.tmdb_language,
            "include_adult": "false",
            "page": 1,
        }
        if year:
            params["first_air_date_year"] = year
        payload = await self._get("/search/tv", **params)
        return [self._normalize_search_result(item, "tv") for item in payload.get("results", [])]

    async def search(self, media_type: str, query: str, year: int | None = None) -> list[dict[str, Any]]:
        if media_type == "movie":
            return await self.search_movie(query, year)
        if media_type == "tv":
            return await self.search_tv(query, year)
        raise ValueError(f"Unknown media type: {media_type}")

    async def movie_details(self, tmdb_id: int) -> dict[str, Any]:
        common = {
            "language": self.settings.tmdb_language,
            "append_to_response": "credits,external_ids,release_dates",
        }
        localized = await self._get(f"/movie/{tmdb_id}", **common)
        if self.settings.tmdb_fallback_language != self.settings.tmdb_language:
            fallback = await self._get(
                f"/movie/{tmdb_id}",
                language=self.settings.tmdb_fallback_language,
                append_to_response="credits,external_ids,release_dates",
            )
            self._merge_missing(
                localized,
                fallback,
                ("title", "original_title", "overview", "tagline", "release_date"),
            )
            if not localized.get("credits"):
                localized["credits"] = fallback.get("credits")
            if not localized.get("external_ids"):
                localized["external_ids"] = fallback.get("external_ids")
        localized["images"] = await self._get(
            f"/movie/{tmdb_id}/images",
            include_image_language=self._image_language_param(),
        )
        localized["media_type"] = "movie"
        return localized

    async def tv_details(self, tmdb_id: int) -> dict[str, Any]:
        localized = await self._get(
            f"/tv/{tmdb_id}",
            language=self.settings.tmdb_language,
            append_to_response="credits,external_ids,content_ratings",
        )
        if self.settings.tmdb_fallback_language != self.settings.tmdb_language:
            fallback = await self._get(
                f"/tv/{tmdb_id}",
                language=self.settings.tmdb_fallback_language,
                append_to_response="credits,external_ids,content_ratings",
            )
            self._merge_missing(
                localized,
                fallback,
                ("name", "original_name", "overview", "tagline", "first_air_date"),
            )
            if not localized.get("credits"):
                localized["credits"] = fallback.get("credits")
            if not localized.get("external_ids"):
                localized["external_ids"] = fallback.get("external_ids")
        localized["images"] = await self._get(
            f"/tv/{tmdb_id}/images",
            include_image_language=self._image_language_param(),
        )
        localized["media_type"] = "tv"
        return localized

    async def season_details(self, tmdb_id: int, season: int) -> dict[str, Any]:
        localized = await self._get(
            f"/tv/{tmdb_id}/season/{season}", language=self.settings.tmdb_language
        )
        if self.settings.tmdb_fallback_language != self.settings.tmdb_language:
            fallback = await self._get(
                f"/tv/{tmdb_id}/season/{season}",
                language=self.settings.tmdb_fallback_language,
            )
            self._merge_missing(localized, fallback, ("name", "overview", "air_date"))
        return localized

    async def episode_details(self, tmdb_id: int, season: int, episode: int) -> dict[str, Any]:
        localized = await self._get(
            f"/tv/{tmdb_id}/season/{season}/episode/{episode}",
            language=self.settings.tmdb_language,
            append_to_response="credits,external_ids",
        )
        if self.settings.tmdb_fallback_language != self.settings.tmdb_language:
            fallback = await self._get(
                f"/tv/{tmdb_id}/season/{season}/episode/{episode}",
                language=self.settings.tmdb_fallback_language,
                append_to_response="credits,external_ids",
            )
            self._merge_missing(localized, fallback, ("name", "overview", "air_date"))
            if not localized.get("credits"):
                localized["credits"] = fallback.get("credits")
        return localized

    def _image_language_param(self) -> str:
        primary = self.settings.tmdb_language.split("-")[0]
        fallback = self.settings.tmdb_fallback_language.split("-")[0]
        values: list[str] = []
        for value in (primary, fallback, "null"):
            if value not in values:
                values.append(value)
        return ",".join(values)

    async def image_url(self, file_path: str | None, size: str = "original") -> str | None:
        if not file_path:
            return None
        config = await self.configuration()
        base = config.get("images", {}).get("secure_base_url") or "https://image.tmdb.org/t/p/"
        return f"{base}{size}{file_path}"

    async def download_image(
        self,
        file_path: str | None,
        destination: Path,
        *,
        size: str = "original",
        force_png: bool = False,
    ) -> bool:
        if not file_path:
            return False
        effective_path = file_path
        if force_png and effective_path.lower().endswith(".svg"):
            effective_path = f"{effective_path[:-4]}.png"
        url = await self.image_url(effective_path, size)
        if not url:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_suffix(destination.suffix + ".part")
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            temp_path.write_bytes(response.content)
            temp_path.replace(destination)
            return True
        except (httpx.HTTPError, OSError) as exc:
            LOGGER.warning("Artwork download failed for %s: %s", url, exc)
            temp_path.unlink(missing_ok=True)
            return False


def pick_image(
    images: list[dict[str, Any]] | None,
    preferred_languages: tuple[str, ...],
) -> str | None:
    if not images:
        return None
    language_order = {language: index for index, language in enumerate(preferred_languages)}

    def sort_key(item: dict[str, Any]) -> tuple[int, float, int, int]:
        language = item.get("iso_639_1")
        lang_rank = language_order.get(language, len(language_order) + 1)
        return (
            -lang_rank,
            float(item.get("vote_average") or 0.0),
            int(item.get("vote_count") or 0),
            int(item.get("width") or 0),
        )

    chosen = max(images, key=sort_key)
    return chosen.get("file_path")

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz

from .config import Settings
from .parser import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS, normalize_for_match

LOGGER = logging.getLogger(__name__)
_FOLDER_PATTERN = re.compile(r"^(?P<title>.+?)\s*\((?P<year>(?:19|20)\d{2})\)\s*$")
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True, slots=True)
class ExistingMedia:
    """One top-level movie or TV-show folder in an existing library.

    The first six fields are also used by the ingest matcher. The remaining
    fields power the read-only library browser.
    """

    path: Path
    title: str
    year: int | None
    tmdb_id: int | None
    media_type: str
    library: str
    item_id: str
    original_title: str | None
    sort_title: str
    imdb_id: str | None
    plot: str
    rating: float | None
    genres: tuple[str, ...]
    nfo_path: Path | None
    poster_path: Path | None
    fanart_path: Path | None
    modified_at: float


@dataclass(frozen=True, slots=True)
class LibraryMediaFile:
    relative_path: str
    size_bytes: int
    modified_at: float


@dataclass(frozen=True, slots=True)
class LibraryDetails:
    item: ExistingMedia
    video_count: int
    season_count: int
    subtitle_count: int
    nfo_count: int
    image_count: int
    total_size_bytes: int
    media_files: tuple[LibraryMediaFile, ...]
    media_files_truncated: bool


class LibraryIndex:
    """Cached, read-only index of Filme, Serien and Animes.

    Only the top-level folders and their movie.nfo/tvshow.nfo are read for the
    main library view. Deep episode/file scanning happens only when opening an
    individual detail page, avoiding a full recursive walk of a large NAS on
    every request.
    """

    def __init__(self, settings: Settings, ttl_seconds: int | None = None):
        self.settings = settings
        self.ttl_seconds = (
            settings.library_cache_seconds if ttl_seconds is None else ttl_seconds
        )
        self._items: list[ExistingMedia] = []
        self._items_by_id: dict[str, ExistingMedia] = {}
        self._built_at = 0.0
        self._built_at_epoch: float | None = None
        self._lock = threading.RLock()

    @property
    def built_at_epoch(self) -> float | None:
        return self._built_at_epoch

    def invalidate(self) -> None:
        with self._lock:
            self._built_at = 0.0

    @staticmethod
    def _folder_title(path: Path) -> tuple[str, int | None]:
        match = _FOLDER_PATTERN.match(path.name)
        if not match:
            return path.name, None
        return match.group("title").strip(), int(match.group("year"))

    @staticmethod
    def _text(root: ET.Element, *names: str) -> str | None:
        for name in names:
            value = root.findtext(name)
            if value and value.strip():
                return value.strip()
        return None

    @classmethod
    def _read_nfo(cls, nfo_path: Path) -> dict[str, object]:
        if not nfo_path.is_file():
            return {}
        try:
            root = ET.parse(nfo_path).getroot()
        except (ET.ParseError, OSError) as exc:
            LOGGER.debug("Could not read NFO %s: %s", nfo_path, exc)
            return {}

        tmdb_id: int | None = None
        imdb_id: str | None = None
        for unique_id in root.findall("uniqueid"):
            value = (unique_id.text or "").strip()
            id_type = (unique_id.get("type") or "").casefold()
            if not value:
                continue
            if id_type == "tmdb":
                try:
                    tmdb_id = int(value)
                except ValueError:
                    pass
            elif id_type == "imdb":
                imdb_id = value

        if tmdb_id is None:
            value = cls._text(root, "tmdbid")
            if value:
                try:
                    tmdb_id = int(value)
                except ValueError:
                    pass
        if imdb_id is None:
            imdb_id = cls._text(root, "imdbid")

        year: int | None = None
        year_value = cls._text(root, "year", "premiered", "releasedate")
        if year_value and len(year_value) >= 4:
            try:
                year = int(year_value[:4])
            except ValueError:
                pass

        rating: float | None = None
        rating_value = cls._text(root, "rating", "ratings/rating/value")
        if rating_value:
            try:
                rating = float(rating_value.replace(",", "."))
            except ValueError:
                pass

        genres = tuple(
            value.strip()
            for node in root.findall("genre")
            if (value := (node.text or "").strip())
        )

        return {
            "title": cls._text(root, "title"),
            "original_title": cls._text(root, "originaltitle", "original_title"),
            "sort_title": cls._text(root, "sorttitle"),
            "year": year,
            "tmdb_id": tmdb_id,
            "imdb_id": imdb_id,
            "plot": cls._text(root, "plot", "outline") or "",
            "rating": rating,
            "genres": genres,
        }

    @staticmethod
    def _first_existing(folder: Path, names: tuple[str, ...]) -> Path | None:
        for name in names:
            candidate = folder / name
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None

    @staticmethod
    def _item_id(library: str, path: Path) -> str:
        digest = hashlib.sha256(f"{library}\0{path.name}".encode("utf-8")).hexdigest()
        return digest[:20]

    def _make_item(
        self,
        *,
        library: str,
        media_type: str,
        folder: Path,
        nfo_name: str,
    ) -> ExistingMedia:
        folder_title, folder_year = self._folder_title(folder)
        nfo_candidate = folder / nfo_name
        metadata = self._read_nfo(nfo_candidate)

        title = str(metadata.get("title") or folder_title).strip() or folder_title
        original_title = metadata.get("original_title")
        original_title = str(original_title).strip() if original_title else None
        year = metadata.get("year") or folder_year
        sort_title = str(metadata.get("sort_title") or title).strip() or title

        poster = self._first_existing(
            folder,
            (
                "poster.jpg",
                "poster.png",
                "folder.jpg",
                "folder.png",
                "cover.jpg",
                "cover.png",
                "thumb.jpg",
            ),
        )
        fanart = self._first_existing(
            folder,
            (
                "fanart.jpg",
                "fanart.png",
                "backdrop.jpg",
                "backdrop.png",
                "background.jpg",
            ),
        )

        modified_values: list[float] = []
        for candidate in (folder, nfo_candidate, poster, fanart):
            if candidate is None:
                continue
            try:
                modified_values.append(candidate.stat().st_mtime)
            except OSError:
                pass

        return ExistingMedia(
            path=folder,
            title=title,
            year=int(year) if isinstance(year, int) else folder_year,
            tmdb_id=metadata.get("tmdb_id") if isinstance(metadata.get("tmdb_id"), int) else None,
            media_type=media_type,
            library=library,
            item_id=self._item_id(library, folder),
            original_title=original_title,
            sort_title=sort_title,
            imdb_id=str(metadata.get("imdb_id")) if metadata.get("imdb_id") else None,
            plot=str(metadata.get("plot") or ""),
            rating=metadata.get("rating") if isinstance(metadata.get("rating"), float) else None,
            genres=tuple(metadata.get("genres") or ()),
            nfo_path=nfo_candidate if nfo_candidate.is_file() else None,
            poster_path=poster,
            fanart_path=fanart,
            modified_at=max(modified_values, default=0.0),
        )

    def _build(self) -> None:
        items: list[ExistingMedia] = []
        roots = (
            ("movies", "movie", self.settings.movie_root, "movie.nfo"),
            ("series", "tv", self.settings.tv_root, "tvshow.nfo"),
            # Animes is exclusively a movie library. Anime TV shows live in Serien.
            ("anime", "movie", self.settings.anime_root, "movie.nfo"),
        )
        for library, media_type, root, nfo_name in roots:
            try:
                children = [
                    entry
                    for entry in root.iterdir()
                    if entry.is_dir() and not entry.name.startswith((".", "_"))
                ]
            except OSError as exc:
                LOGGER.warning("Could not scan library root %s: %s", root, exc)
                continue
            for child in children:
                try:
                    items.append(
                        self._make_item(
                            library=library,
                            media_type=media_type,
                            folder=child,
                            nfo_name=nfo_name,
                        )
                    )
                except OSError as exc:
                    LOGGER.debug("Could not index %s: %s", child, exc)

        items.sort(key=lambda item: normalize_for_match(item.sort_title))
        with self._lock:
            self._items = items
            self._items_by_id = {item.item_id: item for item in items}
            self._built_at = time.monotonic()
            self._built_at_epoch = time.time()

    def refresh(self) -> list[ExistingMedia]:
        self._build()
        with self._lock:
            return list(self._items)

    def items(self) -> list[ExistingMedia]:
        with self._lock:
            stale = time.monotonic() - self._built_at > self.ttl_seconds
        if stale:
            self._build()
        with self._lock:
            return list(self._items)

    def get(self, item_id: str) -> ExistingMedia | None:
        self.items()
        with self._lock:
            return self._items_by_id.get(item_id)

    def counts(self) -> dict[str, int]:
        counts = {"all": 0, "movies": 0, "series": 0, "anime": 0}
        for item in self.items():
            counts[item.library] = counts.get(item.library, 0) + 1
            counts["all"] += 1
        return counts

    def browse(
        self,
        *,
        library: str | None = None,
        query: str = "",
        sort: str = "title_asc",
    ) -> list[ExistingMedia]:
        items = self.items()
        if library in {"movies", "series", "anime"}:
            items = [item for item in items if item.library == library]

        query = query.strip()
        if query:
            normalized_query = normalize_for_match(query)
            items = [
                item
                for item in items
                if normalized_query in normalize_for_match(item.title)
                or normalized_query in normalize_for_match(item.original_title or "")
                or normalized_query in normalize_for_match(item.path.name)
            ]

        if sort == "title_desc":
            items.sort(key=lambda item: normalize_for_match(item.sort_title), reverse=True)
        elif sort == "year_desc":
            items.sort(
                key=lambda item: (item.year is not None, item.year or 0, normalize_for_match(item.sort_title)),
                reverse=True,
            )
        elif sort == "year_asc":
            items.sort(
                key=lambda item: (item.year is None, item.year or 9999, normalize_for_match(item.sort_title))
            )
        elif sort == "updated_desc":
            items.sort(key=lambda item: item.modified_at, reverse=True)
        else:
            items.sort(key=lambda item: normalize_for_match(item.sort_title))
        return items

    def inspect(self, item_id: str, max_media_files: int = 500) -> LibraryDetails | None:
        item = self.get(item_id)
        if item is None:
            return None

        video_files: list[LibraryMediaFile] = []
        video_count = subtitle_count = nfo_count = image_count = 0
        total_size = 0
        seasons: set[str] = set()

        try:
            candidates = item.path.rglob("*")
            for path in candidates:
                try:
                    if not path.is_file():
                        continue
                    relative = path.relative_to(item.path)
                    if any(part.startswith(".") for part in relative.parts):
                        continue
                    stat = path.stat()
                except (OSError, ValueError):
                    continue

                total_size += stat.st_size
                suffix = path.suffix.casefold()
                if suffix in VIDEO_EXTENSIONS:
                    video_count += 1
                    if len(relative.parts) > 1:
                        seasons.add(relative.parts[0])
                    if len(video_files) < max_media_files:
                        video_files.append(
                            LibraryMediaFile(
                                relative_path=str(relative),
                                size_bytes=stat.st_size,
                                modified_at=stat.st_mtime,
                            )
                        )
                elif suffix in SUBTITLE_EXTENSIONS:
                    subtitle_count += 1
                elif suffix == ".nfo":
                    nfo_count += 1
                elif suffix in _IMAGE_EXTENSIONS:
                    image_count += 1
        except OSError as exc:
            LOGGER.warning("Could not inspect library item %s: %s", item.path, exc)

        video_files.sort(key=lambda file: normalize_for_match(file.relative_path))
        return LibraryDetails(
            item=item,
            video_count=video_count,
            season_count=len(seasons) if item.media_type == "tv" else 0,
            subtitle_count=subtitle_count,
            nfo_count=nfo_count,
            image_count=image_count,
            total_size_bytes=total_size,
            media_files=tuple(video_files),
            media_files_truncated=video_count > len(video_files),
        )

    def find_by_tmdb(
        self, tmdb_id: int, media_type: str, library: str | None = None
    ) -> ExistingMedia | None:
        for item in self.items():
            if item.tmdb_id != tmdb_id or item.media_type != media_type:
                continue
            if library and item.library != library:
                continue
            return item
        return None

    def find_best(
        self,
        title: str,
        year: int | None,
        media_type: str,
        preferred_library: str | None = None,
        threshold: float = 0.94,
    ) -> ExistingMedia | None:
        query = normalize_for_match(title)
        best: ExistingMedia | None = None
        best_score = 0.0
        for item in self.items():
            if item.media_type != media_type:
                continue
            if preferred_library and item.library != preferred_library:
                continue
            score = fuzz.ratio(query, normalize_for_match(item.title)) / 100.0
            if year and item.year:
                delta = abs(year - item.year)
                if delta == 0:
                    score += 0.04
                elif delta == 1:
                    score += 0.01
                else:
                    score -= 0.08
            if score > best_score:
                best_score = score
                best = item
        return best if best and best_score >= threshold else None


def format_timestamp(value: float | None) -> str:
    if not value:
        return "–"
    return datetime.fromtimestamp(value).strftime("%d.%m.%Y %H:%M")

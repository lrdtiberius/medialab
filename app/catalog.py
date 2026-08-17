from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .db import Database
from .parser import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS

LOGGER = logging.getLogger(__name__)
_FOLDER_PATTERN = re.compile(r"^(?P<title>.+?)\s*\((?P<year>(?:19|20)\d{2})\)\s*$")
_SEASON_PATTERN = re.compile(r"(?i)^(?:season|staffel)\s*0*(?P<number>\d+)$")
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True, slots=True)
class TechnicalAggregate:
    probed_files: int = 0
    resolutions: tuple[str, ...] = ()
    video_codecs: tuple[str, ...] = ()
    hdr_formats: tuple[str, ...] = ()
    audio_codecs: tuple[str, ...] = ()
    audio_languages: tuple[str, ...] = ()
    channel_layouts: tuple[str, ...] = ()
    audio_profiles: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True, slots=True)
class CatalogItem:
    item_id: str
    path: Path
    relative_path: str
    title: str
    original_title: str
    year: int | None
    premiered: str
    overview: str
    runtime_minutes: int | None
    rating: float | None
    genres: tuple[str, ...]
    tmdb_id: int | None
    imdb_id: str
    media_type: str
    library: str
    nfo_path: Path | None
    poster_path: Path | None
    backdrop_path: Path | None
    modified_at: float
    technical: TechnicalAggregate = field(default_factory=TechnicalAggregate)

    @property
    def has_nfo(self) -> bool:
        return self.nfo_path is not None

    @property
    def has_poster(self) -> bool:
        return self.poster_path is not None


@dataclass(frozen=True, slots=True)
class CatalogFile:
    name: str
    relative_path: str
    size_bytes: int
    modified_at: float
    kind: str
    technical_summary: str = ""
    technical_info: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CatalogDetails:
    item: CatalogItem
    files: tuple[CatalogFile, ...]
    files_truncated: bool
    total_size: int
    video_count: int
    subtitle_count: int
    nfo_count: int
    artwork_count: int
    season_count: int
    episode_count: int
    technical: TechnicalAggregate
    unprobed_video_count: int


def _sorted_values(values: set[str]) -> tuple[str, ...]:
    return tuple(sorted((value for value in values if value), key=str.casefold))


def aggregate_probes(payloads: Iterable[dict[str, Any]]) -> TechnicalAggregate:
    resolutions: set[str] = set()
    video_codecs: set[str] = set()
    hdr_formats: set[str] = set()
    audio_codecs: set[str] = set()
    audio_languages: set[str] = set()
    channel_layouts: set[str] = set()
    audio_profiles: set[str] = set()
    count = 0

    for payload in payloads:
        if not payload:
            continue
        count += 1
        video = payload.get("video") or {}
        if video.get("resolution") and video.get("resolution") != "Unbekannt":
            resolutions.add(str(video["resolution"]))
        if video.get("codec"):
            video_codecs.add(str(video["codec"]))
        if video.get("hdr"):
            hdr_formats.add(str(video["hdr"]))
        for track in payload.get("audio") or []:
            if not isinstance(track, dict):
                continue
            if track.get("codec"):
                audio_codecs.add(str(track["codec"]))
            if track.get("language") and track.get("language") not in {"–", "UND"}:
                audio_languages.add(str(track["language"]))
            if track.get("channel_label") and track.get("channel_label") != "–":
                channel_layouts.add(str(track["channel_label"]))
            if track.get("summary"):
                audio_profiles.add(str(track["summary"]))

    resolution_values = _sorted_values(resolutions)
    codec_values = _sorted_values(video_codecs)
    hdr_values = _sorted_values(hdr_formats)
    audio_codec_values = _sorted_values(audio_codecs)
    language_values = _sorted_values(audio_languages)
    channel_values = _sorted_values(channel_layouts)
    profile_values = _sorted_values(audio_profiles)

    summary_parts: list[str] = []
    if resolution_values:
        summary_parts.append("/".join(resolution_values[:3]))
    if codec_values:
        summary_parts.append("/".join(codec_values[:3]))
    if hdr_values:
        summary_parts.append("/".join(hdr_values[:2]))
    if profile_values:
        summary_parts.append(" / ".join(profile_values[:2]))
        if len(profile_values) > 2:
            summary_parts.append(f"+{len(profile_values) - 2} Audio")

    return TechnicalAggregate(
        probed_files=count,
        resolutions=resolution_values,
        video_codecs=codec_values,
        hdr_formats=hdr_values,
        audio_codecs=audio_codec_values,
        audio_languages=language_values,
        channel_layouts=channel_values,
        audio_profiles=profile_values,
        summary=" · ".join(summary_parts),
    )


class MediaCatalog:
    """Read-only catalogue for existing media folders and cached stream data."""

    def __init__(
        self,
        settings: Settings,
        database: Database | None = None,
        ttl_seconds: int | None = None,
    ):
        self.settings = settings
        self.database = database or Database(settings.database_path)
        self.database.initialize()
        self.ttl_seconds = ttl_seconds or settings.library_cache_seconds
        self._items: tuple[CatalogItem, ...] = ()
        self._by_id: dict[str, CatalogItem] = {}
        self._built_at = 0.0
        self._lock = threading.RLock()
        self._last_error = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def built_at(self) -> float:
        return self._built_at

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
    def _stable_id(library: str, path: Path) -> str:
        raw = f"{library}\0{path.name}".encode("utf-8", errors="surrogatepass")
        return hashlib.sha256(raw).hexdigest()[:24]

    @staticmethod
    def _safe_text(root: ET.Element, path: str) -> str:
        node = root.find(path)
        if node is None or node.text is None:
            return ""
        return node.text.strip()

    @classmethod
    def _parse_nfo(cls, path: Path) -> dict[str, object]:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as exc:
            LOGGER.debug("Could not parse NFO %s: %s", path, exc)
            return {}

        tmdb_id: int | None = None
        imdb_id = ""
        for unique_id in root.findall("uniqueid"):
            kind = (unique_id.get("type") or "").strip().lower()
            value = (unique_id.text or "").strip()
            if not value:
                continue
            if kind == "tmdb":
                try:
                    tmdb_id = int(value)
                except ValueError:
                    pass
            elif kind == "imdb":
                imdb_id = value

        if tmdb_id is None:
            value = cls._safe_text(root, "tmdbid")
            try:
                tmdb_id = int(value) if value else None
            except ValueError:
                tmdb_id = None

        year: int | None = None
        year_text = cls._safe_text(root, "year")
        if year_text:
            try:
                year = int(year_text[:4])
            except ValueError:
                year = None

        runtime: int | None = None
        runtime_text = cls._safe_text(root, "runtime")
        if runtime_text:
            try:
                runtime = int(float(runtime_text))
            except ValueError:
                runtime = None

        rating: float | None = None
        rating_text = (
            cls._safe_text(root, "ratings/rating[@default='true']/value")
            or cls._safe_text(root, "ratings/rating/value")
            or cls._safe_text(root, "rating")
        )
        if rating_text:
            try:
                rating = float(rating_text)
            except ValueError:
                rating = None

        genres = tuple(
            node.text.strip()
            for node in root.findall("genre")
            if node.text and node.text.strip()
        )

        return {
            "title": cls._safe_text(root, "title"),
            "original_title": cls._safe_text(root, "originaltitle"),
            "year": year,
            "premiered": cls._safe_text(root, "premiered"),
            "overview": cls._safe_text(root, "plot") or cls._safe_text(root, "outline"),
            "runtime_minutes": runtime,
            "rating": rating,
            "genres": genres,
            "tmdb_id": tmdb_id,
            "imdb_id": imdb_id,
        }

    @staticmethod
    def _first_existing(folder: Path, names: Iterable[str]) -> Path | None:
        for name in names:
            candidate = folder / name
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None

    @staticmethod
    def _nfo_path(folder: Path, media_type: str) -> Path | None:
        preferred = folder / ("movie.nfo" if media_type == "movie" else "tvshow.nfo")
        try:
            if preferred.is_file():
                return preferred
        except OSError:
            return None
        if media_type == "movie":
            try:
                candidates = sorted(folder.glob("*.nfo"))
            except OSError:
                return None
            return candidates[0] if candidates else None
        return None

    def _probe_aggregates(self) -> dict[tuple[str, str], TechnicalAggregate]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        roots = {
            "movies": self.settings.movie_root,
            "series": self.settings.tv_root,
            "anime": self.settings.anime_root,
        }
        for row in self.database.list_media_probes():
            if row.get("error"):
                continue
            path = Path(row["file_path"])
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size != row.get("size_bytes") or abs(stat.st_mtime - float(row.get("mtime") or 0)) > 0.000001:
                continue
            for library, root in roots.items():
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    continue
                if not relative.parts:
                    continue
                grouped.setdefault((library, relative.parts[0]), []).append(row.get("payload") or {})
                break
        return {key: aggregate_probes(payloads) for key, payloads in grouped.items()}

    def _scan_root(
        self,
        library: str,
        media_type: str,
        root: Path,
        probe_aggregates: dict[tuple[str, str], TechnicalAggregate],
    ) -> list[CatalogItem]:
        try:
            children = sorted(
                (
                    entry
                    for entry in root.iterdir()
                    if entry.is_dir() and not entry.name.startswith(".")
                ),
                key=lambda item: item.name.casefold(),
            )
        except OSError as exc:
            LOGGER.warning("Could not read library %s at %s: %s", library, root, exc)
            self._last_error = f"{root}: {exc}"
            return []

        items: list[CatalogItem] = []
        for folder in children:
            fallback_title, fallback_year = self._folder_title(folder)
            nfo_path = self._nfo_path(folder, media_type)
            metadata = self._parse_nfo(nfo_path) if nfo_path else {}
            title = str(metadata.get("title") or fallback_title).strip() or fallback_title
            original_title = str(metadata.get("original_title") or "").strip()
            year = metadata.get("year") if isinstance(metadata.get("year"), int) else fallback_year

            poster = self._first_existing(
                folder,
                ("poster.jpg", "poster.png", "folder.jpg", "folder.png", "cover.jpg", "cover.png"),
            )
            backdrop_names = (
                ("backdrop.jpg", "backdrop.png", "fanart.jpg", "fanart.png", "landscape.jpg")
                if media_type == "movie"
                else ("fanart.jpg", "fanart.png", "backdrop.jpg", "backdrop.png", "thumb.jpg")
            )
            backdrop = self._first_existing(folder, backdrop_names)
            try:
                modified_at = folder.stat().st_mtime
            except OSError:
                modified_at = 0.0

            items.append(
                CatalogItem(
                    item_id=self._stable_id(library, folder),
                    path=folder,
                    relative_path=str(folder.relative_to(self.settings.media_root)),
                    title=title,
                    original_title=original_title,
                    year=year,
                    premiered=str(metadata.get("premiered") or ""),
                    overview=str(metadata.get("overview") or ""),
                    runtime_minutes=(
                        int(metadata["runtime_minutes"])
                        if isinstance(metadata.get("runtime_minutes"), int)
                        else None
                    ),
                    rating=(
                        float(metadata["rating"])
                        if isinstance(metadata.get("rating"), (int, float))
                        else None
                    ),
                    genres=tuple(metadata.get("genres") or ()),
                    tmdb_id=(
                        int(metadata["tmdb_id"])
                        if isinstance(metadata.get("tmdb_id"), int)
                        else None
                    ),
                    imdb_id=str(metadata.get("imdb_id") or ""),
                    media_type=media_type,
                    library=library,
                    nfo_path=nfo_path,
                    poster_path=poster,
                    backdrop_path=backdrop,
                    modified_at=modified_at,
                    technical=probe_aggregates.get((library, folder.name), TechnicalAggregate()),
                )
            )
        return items

    def refresh(self, force: bool = False) -> tuple[CatalogItem, ...]:
        with self._lock:
            age = time.monotonic() - self._built_at
            if not force and self._items and age <= self.ttl_seconds:
                return self._items

            self._last_error = ""
            items: list[CatalogItem] = []
            probe_aggregates = self._probe_aggregates()
            roots = (
                ("movies", "movie", self.settings.movie_root),
                ("series", "tv", self.settings.tv_root),
                ("anime", "movie", self.settings.anime_root),
            )
            for library, media_type, root in roots:
                try:
                    exists = root.exists()
                except OSError:
                    exists = False
                if exists:
                    items.extend(self._scan_root(library, media_type, root, probe_aggregates))

            items.sort(key=lambda item: (item.title.casefold(), item.year or 0, item.library))
            self._items = tuple(items)
            self._by_id = {item.item_id: item for item in items}
            self._built_at = time.monotonic()
            return self._items

    def items(self) -> tuple[CatalogItem, ...]:
        return self.refresh(force=False)

    def get(self, item_id: str) -> CatalogItem | None:
        self.refresh(force=False)
        return self._by_id.get(item_id)

    def counts(self) -> dict[str, int]:
        items = self.items()
        result = {"all": len(items), "movies": 0, "series": 0, "anime": 0}
        for item in items:
            result[item.library] = result.get(item.library, 0) + 1
        return result

    @staticmethod
    def _classify_file(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            return "video"
        if suffix in SUBTITLE_EXTENSIONS:
            return "subtitle"
        if suffix == ".nfo":
            return "nfo"
        if suffix in _IMAGE_EXTENSIONS:
            return "artwork"
        return "other"

    def details(self, item_id: str, max_files: int = 500) -> CatalogDetails | None:
        item = self.get(item_id)
        if item is None:
            return None

        probe_rows = {
            row["file_path"]: row
            for row in self.database.list_media_probes(prefix=item.path)
            if not row.get("error")
        }
        valid_payloads: list[dict[str, Any]] = []
        files: list[CatalogFile] = []
        scanned_file_count = 0
        total_size = 0
        video_count = 0
        subtitle_count = 0
        nfo_count = 0
        artwork_count = 0
        seasons: set[int] = set()

        try:
            paths = sorted(
                (path for path in item.path.rglob("*") if path.is_file()),
                key=lambda path: str(path.relative_to(item.path)).casefold(),
            )
        except OSError as exc:
            LOGGER.warning("Could not inspect media folder %s: %s", item.path, exc)
            paths = []

        for path in paths:
            scanned_file_count += 1
            kind = self._classify_file(path)
            try:
                stat = path.stat()
                size = stat.st_size
                modified_at = stat.st_mtime
            except OSError:
                size = 0
                modified_at = 0.0
            total_size += size
            technical_info: dict[str, Any] = {}
            technical_summary = ""
            if kind == "video":
                video_count += 1
                row = probe_rows.get(str(path.resolve()))
                if row and row.get("size_bytes") == size and abs(float(row.get("mtime") or 0) - modified_at) <= 0.000001:
                    technical_info = row.get("payload") or {}
                    technical_summary = str(technical_info.get("summary") or "")
                    valid_payloads.append(technical_info)
            elif kind == "subtitle":
                subtitle_count += 1
            elif kind == "nfo":
                nfo_count += 1
            elif kind == "artwork":
                artwork_count += 1

            for parent in path.parents:
                if parent == item.path:
                    break
                match = _SEASON_PATTERN.match(parent.name)
                if match:
                    seasons.add(int(match.group("number")))
                    break

            if len(files) < max_files:
                files.append(
                    CatalogFile(
                        name=path.name,
                        relative_path=str(path.relative_to(item.path)),
                        size_bytes=size,
                        modified_at=modified_at,
                        kind=kind,
                        technical_summary=technical_summary,
                        technical_info=technical_info,
                    )
                )

        technical = aggregate_probes(valid_payloads)
        return CatalogDetails(
            item=item,
            files=tuple(files),
            files_truncated=scanned_file_count > len(files),
            total_size=total_size,
            video_count=video_count,
            subtitle_count=subtitle_count,
            nfo_count=nfo_count,
            artwork_count=artwork_count,
            season_count=len(seasons),
            episode_count=video_count if item.media_type == "tv" else 0,
            technical=technical,
            unprobed_video_count=max(0, video_count - technical.probed_files),
        )

    def artwork(self, item_id: str, kind: str) -> Path | None:
        item = self.get(item_id)
        if item is None:
            return None
        if kind == "poster":
            candidate = item.poster_path
        elif kind == "backdrop":
            candidate = item.backdrop_path
        else:
            return None
        if candidate is None:
            return None
        try:
            resolved = candidate.resolve()
            resolved.relative_to(item.path.resolve())
            return resolved if resolved.is_file() else None
        except (OSError, ValueError):
            return None

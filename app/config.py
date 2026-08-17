from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__


_TMDB_CREDENTIAL_NAMES = {"TMDB_READ_TOKEN", "TMDB_API_KEY"}


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "ja"}


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    value = default if raw is None else int(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _float(name: str, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    raw = os.getenv(name)
    value = default if raw is None else float(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _strip_optional_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_credentials_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name in _TMDB_CREDENTIAL_NAMES:
            values[name] = _strip_optional_quotes(value)
    return values


def _tmdb_credentials(media_root: Path, inbox_root: Path) -> tuple[str, str, Path]:
    explicit = os.getenv("TMDB_CREDENTIALS_FILE", "").strip()
    if explicit:
        configured_path = Path(explicit).expanduser()
        if not configured_path.is_absolute():
            configured_path = media_root / configured_path
        candidates = [configured_path.resolve()]
    else:
        candidates = [
            (media_root / "_config" / "tmdb.env").resolve(),
            (media_root / "_config" / "tmdb.txt").resolve(),
            (inbox_root / "_config" / "tmdb.env").resolve(),
            (inbox_root / "_config" / "tmdb.txt").resolve(),
        ]
    selected_path = candidates[0]
    first_existing: Path | None = None
    file_values: dict[str, str] = {}
    for candidate in candidates:
        values = _read_credentials_file(candidate)
        if values:
            selected_path = candidate
            file_values = values
            break
        try:
            exists = candidate.is_file()
        except (PermissionError, OSError):
            exists = False
        if exists and first_existing is None:
            first_existing = candidate
    else:
        if first_existing is not None:
            selected_path = first_existing
    read_token = file_values.get("TMDB_READ_TOKEN", "").strip() or os.getenv("TMDB_READ_TOKEN", "").strip()
    api_key = file_values.get("TMDB_API_KEY", "").strip() or os.getenv("TMDB_API_KEY", "").strip()
    return read_token, api_key, selected_path


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str
    app_version: str
    build_id: str
    app_port: int
    media_root: Path
    inbox_root: Path
    movie_root: Path
    tv_root: Path
    anime_root: Path
    data_root: Path
    database_path: Path
    staging_root: Path
    tmdb_read_token: str = field(repr=False)
    tmdb_api_key: str = field(repr=False)
    tmdb_credentials_file: Path
    tmdb_language: str
    tmdb_fallback_language: str
    tmdb_region: str
    scan_interval_seconds: int
    file_stable_seconds: int
    file_stable_min_checks: int
    file_stable_mtime_seconds: int
    auto_match_threshold: float
    auto_match_margin: float
    auto_process: bool
    dry_run: bool
    anime_auto_detect: bool
    anime_languages: tuple[str, ...]
    overwrite_existing: bool
    overwrite_metadata: bool
    create_nfo: bool
    duplicate_movie_nfo: bool
    download_artwork: bool
    create_derived_artwork: bool
    keep_technical_tags: bool
    tv_keep_technical_tags: bool
    ffprobe_enabled: bool
    ffprobe_path: str
    ffprobe_timeout_seconds: int
    library_probe_concurrency: int
    library_probe_auto_start: bool
    author_name: str
    support_url: str
    web_username: str
    web_password: str
    log_level: str
    history_limit: int
    library_cache_seconds: int
    library_page_size: int
    log_tail_lines: int

    @property
    def app_author(self) -> str:
        return self.author_name

    @property
    def tmdb_configured(self) -> bool:
        return bool(self.tmdb_read_token or self.tmdb_api_key)

    @property
    def libraries(self) -> dict[str, Path]:
        return {"movies": self.movie_root, "series": self.tv_root, "anime": self.anime_root}

    def library_path(self, library: str) -> Path:
        try:
            return self.libraries[library]
        except KeyError as exc:
            raise ValueError(f"Unknown library: {library}") from exc


def load_settings() -> Settings:
    media_root = Path(os.getenv("MEDIA_ROOT", "/media")).resolve()
    data_root = Path(os.getenv("DATA_ROOT", "/data")).resolve()
    inbox_name = os.getenv("INBOX_DIR", "New").strip("/")
    movie_name = os.getenv("MOVIE_DIR", "Filme").strip("/")
    tv_name = os.getenv("TV_DIR", "Serien").strip("/")
    anime_name = os.getenv("ANIME_DIR", "Animes").strip("/")
    inbox_root = (media_root / inbox_name).resolve()
    tmdb_read_token, tmdb_api_key, tmdb_credentials_file = _tmdb_credentials(media_root, inbox_root)

    settings = Settings(
        app_name="MediaLab",
        app_version=__version__,
        build_id=(os.getenv("BUILD_ID", "medialab-0.4.1-source").strip() or "medialab-0.4.1-source"),
        app_port=_int("APP_PORT", 8099, 1),
        media_root=media_root,
        inbox_root=inbox_root,
        movie_root=(media_root / movie_name).resolve(),
        tv_root=(media_root / tv_name).resolve(),
        anime_root=(media_root / anime_name).resolve(),
        data_root=data_root,
        database_path=((data_root / "media-ingest.sqlite3") if (data_root / "media-ingest.sqlite3").exists() else (data_root / "medialab.sqlite3")).resolve(),
        staging_root=(data_root / "staging").resolve(),
        tmdb_read_token=tmdb_read_token,
        tmdb_api_key=tmdb_api_key,
        tmdb_credentials_file=tmdb_credentials_file,
        tmdb_language=os.getenv("TMDB_LANGUAGE", "de-DE").strip() or "de-DE",
        tmdb_fallback_language=os.getenv("TMDB_FALLBACK_LANGUAGE", "en-US").strip() or "en-US",
        tmdb_region=os.getenv("TMDB_REGION", "DE").strip() or "DE",
        scan_interval_seconds=_int("SCAN_INTERVAL_SECONDS", 20, 5),
        file_stable_seconds=_int("FILE_STABLE_SECONDS", 120, 20),
        file_stable_min_checks=_int("FILE_STABLE_MIN_CHECKS", 3, 2),
        file_stable_mtime_seconds=_int("FILE_STABLE_MTIME_SECONDS", 60, 10),
        auto_match_threshold=_float("AUTO_MATCH_THRESHOLD", 0.92, 0.0, 1.0),
        auto_match_margin=_float("AUTO_MATCH_MARGIN", 0.08, 0.0, 1.0),
        auto_process=_bool("AUTO_PROCESS", True),
        dry_run=_bool("DRY_RUN", True),
        anime_auto_detect=_bool("ANIME_AUTO_DETECT", True),
        anime_languages=_csv("ANIME_LANGUAGES", "ja"),
        overwrite_existing=_bool("OVERWRITE_EXISTING", False),
        overwrite_metadata=_bool("OVERWRITE_METADATA", False),
        create_nfo=_bool("CREATE_NFO", True),
        duplicate_movie_nfo=_bool("DUPLICATE_MOVIE_NFO", True),
        download_artwork=_bool("DOWNLOAD_ARTWORK", True),
        create_derived_artwork=_bool("CREATE_DERIVED_ARTWORK", True),
        keep_technical_tags=_bool("KEEP_TECHNICAL_TAGS", True),
        tv_keep_technical_tags=_bool("TV_KEEP_TECHNICAL_TAGS", False),
        ffprobe_enabled=_bool("FFPROBE_ENABLED", True),
        ffprobe_path=os.getenv("FFPROBE_PATH", "ffprobe").strip() or "ffprobe",
        ffprobe_timeout_seconds=_int("FFPROBE_TIMEOUT_SECONDS", 90, 5),
        library_probe_concurrency=_int("LIBRARY_PROBE_CONCURRENCY", 2, 1),
        library_probe_auto_start=_bool("LIBRARY_PROBE_AUTO_START", False),
        author_name=(os.getenv("AUTHOR_NAME") or os.getenv("APP_AUTHOR") or "Lrd.Tiberius").strip() or "Lrd.Tiberius",
        support_url=os.getenv("SUPPORT_URL", "https://www.paypal.com/paypalme/SebastianM207").strip(),
        web_username=os.getenv("WEB_USERNAME", "").strip(),
        web_password=os.getenv("WEB_PASSWORD", "").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO",
        history_limit=_int("HISTORY_LIMIT", 1000, 50),
        library_cache_seconds=_int("LIBRARY_CACHE_SECONDS", 600, 30),
        library_page_size=_int("LIBRARY_PAGE_SIZE", 60, 12),
        log_tail_lines=_int("LOG_TAIL_LINES", 200, 20),
    )
    if bool(settings.web_username) != bool(settings.web_password):
        raise ValueError("WEB_USERNAME and WEB_PASSWORD must either both be set or both be empty")
    if settings.auto_match_margin > settings.auto_match_threshold:
        raise ValueError("AUTO_MATCH_MARGIN cannot be greater than AUTO_MATCH_THRESHOLD")
    roots = [settings.inbox_root, settings.movie_root, settings.tv_root, settings.anime_root]
    if len(set(roots)) != len(roots):
        raise ValueError("New, Filme, Serien and Animes must use different directories")
    for root in roots:
        if settings.media_root not in root.parents:
            raise ValueError(f"Configured directory is outside MEDIA_ROOT: {root}")
    return settings

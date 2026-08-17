from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".ts",
    ".m2ts",
    ".webm",
    ".mpg",
    ".mpeg",
}

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}

_EPISODE_PATTERNS = (
    re.compile(
        r"(?i)(?<![A-Z0-9])S(?P<season>\d{1,3})[ ._-]*E(?P<episode>\d{1,4})"
        r"(?:[ ._-]*(?:E|-E)(?P<episode_end>\d{1,4}))?(?!\d)"
    ),
    re.compile(
        r"(?i)(?<!\d)(?P<season>\d{1,3})x(?P<episode>\d{1,4})"
        r"(?:[ ._-]*(?:-|x)[ ._-]*(?P<episode_end>\d{1,4}))?(?!\d)"
    ),
)

# Typical anime release: "[Group] Show Name - 01 [1080p].mkv".
_ANIME_ABSOLUTE_PATTERN = re.compile(
    r"(?i)^(?P<title>.+?)[ ._]+-[ ._]+(?P<episode>\d{1,4})(?:v\d)?(?:[ ._-]|$)"
)

_YEAR_PATTERN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

_NOISE_WORDS = {
    "bluray",
    "blu-ray",
    "bdrip",
    "brrip",
    "webrip",
    "web-dl",
    "webdl",
    "hdtv",
    "dvdrip",
    "remux",
    "proper",
    "repack",
    "internal",
    "german",
    "deutsch",
    "english",
    "multi",
    "dubbed",
    "subbed",
    "dl",
    "dual",
    "complete",
    "uncut",
    "extended",
    "limited",
    "readnfo",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "av1",
    "10bit",
    "8bit",
    "hdr",
    "hdr10",
    "hdr10plus",
    "dolbyvision",
    "dv",
    "atmos",
    "truehd",
    "eac3",
    "e-ac3",
    "ddp",
    "dd+",
    "ac3",
    "dts",
    "aac",
    "flac",
    "amzn",
    "nf",
    "dsnp",
    "atvp",
}

_RELEASE_GROUPS = {
    "subsplease",
    "erai-raws",
    "horriblesubs",
    "eztv",
    "rarbg",
    "yts",
    "yify",
}

_RESOLUTION_PATTERNS = (
    (re.compile(r"(?i)(?<!\d)(?:3840x2160|2160p|4k)(?!\d)"), "2160p"),
    (re.compile(r"(?i)(?<!\d)(?:1920x1080|1080p)(?!\d)"), "1080p"),
    (re.compile(r"(?i)(?<!\d)(?:1280x720|720p)(?!\d)"), "720p"),
    (re.compile(r"(?i)(?<!\d)576p(?!\d)"), "576p"),
    (re.compile(r"(?i)(?<!\d)480p(?!\d)"), "480p"),
)

_AUDIO_PATTERNS = (
    (re.compile(r"(?i)(?:DTS[ ._-]*HD[ ._-]*MA|DTSHDMA)"), "DTS-HD MA"),
    (re.compile(r"(?i)(?:TRUEHD|TRUE[ ._-]*HD)"), "TrueHD"),
    (re.compile(r"(?i)(?:E[ ._-]*AC3|EAC3|DDP|DD\+)"), "EAC3"),
    (re.compile(r"(?i)(?<![A-Z0-9])DTS(?![A-Z0-9])"), "DTS"),
    (re.compile(r"(?i)(?<![A-Z0-9])AC3(?![A-Z0-9])"), "AC3"),
    (re.compile(r"(?i)(?<![A-Z0-9])AAC(?![A-Z0-9])"), "AAC"),
    (re.compile(r"(?i)(?<![A-Z0-9])FLAC(?![A-Z0-9])"), "FLAC"),
)

_HDR_PATTERNS = (
    (re.compile(r"(?i)(?:DOLBY[ ._-]*VISION|\bDV\b)"), "DV"),
    (re.compile(r"(?i)HDR10\+"), "HDR10+"),
    (re.compile(r"(?i)HDR10"), "HDR10"),
    (re.compile(r"(?i)(?<![A-Z0-9])HDR(?![A-Z0-9])"), "HDR"),
)


@dataclass(slots=True)
class ParsedMedia:
    media_type: str
    title: str
    year: int | None
    season: int | None
    episode: int | None
    episode_end: int | None
    technical_tags: list[str]
    extension: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def is_video_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def normalize_for_match(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def sanitize_component(value: str, fallback: str = "Unbekannt") -> str:
    value = value.replace("/", " - ").replace("\\", " - ")
    value = value.replace(":", " - ")
    value = re.sub(r"[\x00-\x1f<>\"|?*]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or fallback


def extract_technical_tags(name: str) -> list[str]:
    tags: list[str] = []
    for pattern, label in _RESOLUTION_PATTERNS:
        if pattern.search(name):
            tags.append(label)
            break
    for pattern, label in _HDR_PATTERNS:
        if pattern.search(name):
            tags.append(label)
            break
    for pattern, label in _AUDIO_PATTERNS:
        if pattern.search(name):
            tags.append(label)
            break
    return tags


def _clean_bracket_content(match: re.Match[str]) -> str:
    content = match.group(1).strip()
    normalized = normalize_for_match(content)
    if not content:
        return " "
    if normalized in _RELEASE_GROUPS:
        return " "
    if re.fullmatch(r"[A-Fa-f0-9]{7,16}", content):
        return " "
    all_patterns = _RESOLUTION_PATTERNS + _AUDIO_PATTERNS + _HDR_PATTERNS
    if any(pattern.search(content) for pattern, _ in all_patterns):
        return " "
    words = set(normalized.split())
    if words and words.issubset(_NOISE_WORDS):
        return " "
    return f" {content} "


def _remove_release_noise(value: str) -> str:
    value = re.sub(r"\[([^\]]*)\]", _clean_bracket_content, value)
    value = re.sub(r"\(([^)]*)\)", _clean_bracket_content, value)
    value = value.replace("_", " ").replace(".", " ")
    value = re.sub(r"(?i)\b(?:www\.[^ ]+|https?[^ ]+)\b", " ", value)

    for pattern, _ in _RESOLUTION_PATTERNS + _AUDIO_PATTERNS + _HDR_PATTERNS:
        value = pattern.sub(" ", value)

    noise = "|".join(re.escape(item) for item in sorted(_NOISE_WORDS, key=len, reverse=True))
    value = re.sub(rf"(?i)(?<![A-Z0-9])(?:{noise})(?![A-Z0-9])", " ", value)
    value = re.sub(r"(?i)\b(?:WEB[ ._-]?DL|Blu[ ._-]?Ray|HDTV|REMUX)\b", " ", value)
    value = re.sub(r"(?i)\b(?:5[ .]?1|7[ .]?1|2[ .]?0)\b", " ", value)
    value = re.sub(r"(?i)\b(?:GER|DEU|ENG)\b", " ", value)
    value = re.sub(r"\s+-\s*[A-Za-z0-9]{2,15}$", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -._")


def parse_media_filename(path: Path | str, *, anime_hint: bool = False) -> ParsedMedia:
    path = Path(path)
    raw_stem = path.stem
    technical_tags = extract_technical_tags(raw_stem)

    episode_match: re.Match[str] | None = None
    for pattern in _EPISODE_PATTERNS:
        episode_match = pattern.search(raw_stem)
        if episode_match:
            break

    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    media_type = "movie"
    title_area = raw_stem

    if episode_match:
        media_type = "tv"
        season = int(episode_match.group("season"))
        episode = int(episode_match.group("episode"))
        end_value = episode_match.groupdict().get("episode_end")
        episode_end = int(end_value) if end_value else None
        title_area = raw_stem[: episode_match.start()]
    elif anime_hint:
        anime_match = _ANIME_ABSOLUTE_PATTERN.search(raw_stem)
        if anime_match:
            media_type = "tv"
            season = 1
            episode = int(anime_match.group("episode"))
            title_area = anime_match.group("title")

    year: int | None = None
    year_matches = list(_YEAR_PATTERN.finditer(title_area if media_type == "tv" else raw_stem))
    if year_matches:
        year = int(year_matches[-1].group(1))

    if media_type == "movie" and year_matches:
        title_area = raw_stem[: year_matches[-1].start()].rstrip()
        # For names such as "8 Mile (2002) 1080p.mkv", slicing at the year
        # leaves the opening bracket behind. Remove only that unmatched bracket.
        title_area = re.sub(r"[([{]\s*$", "", title_area).rstrip()

    title = _remove_release_noise(title_area)
    title = re.sub(r"\((?:19|20)\d{2}\)", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -._")
    if not title:
        title = _remove_release_noise(raw_stem)
    if not title:
        title = raw_stem

    return ParsedMedia(
        media_type=media_type,
        title=sanitize_component(title),
        year=year,
        season=season,
        episode=episode,
        episode_end=episode_end,
        technical_tags=technical_tags,
        extension=path.suffix.lower(),
    )

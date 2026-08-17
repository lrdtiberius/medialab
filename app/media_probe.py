from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


class ProbeError(RuntimeError):
    """Raised when ffprobe cannot inspect a media file."""


# Descriptive alias used by the service layer.
MediaProbeError = ProbeError


_CODEC_LABELS = {
    "h264": "H.264",
    "avc1": "H.264",
    "hevc": "HEVC",
    "h265": "HEVC",
    "av1": "AV1",
    "vp9": "VP9",
    "vp8": "VP8",
    "mpeg4": "MPEG-4",
    "mpeg2video": "MPEG-2",
    "vc1": "VC-1",
    "prores": "ProRes",
    "mjpeg": "MJPEG",
    "aac": "AAC",
    "ac3": "AC3",
    "eac3": "EAC3",
    "truehd": "TrueHD",
    "dts": "DTS",
    "flac": "FLAC",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "mp3": "MP3",
    "mp2": "MP2",
    "alac": "ALAC",
    "wmav2": "WMA",
    "pcm_s16le": "PCM",
    "pcm_s24le": "PCM",
    "pcm_s32le": "PCM",
    "subrip": "SRT",
    "ass": "ASS",
    "ssa": "SSA",
    "webvtt": "WebVTT",
    "hdmv_pgs_subtitle": "PGS",
    "dvd_subtitle": "VobSub",
}

_LANGUAGE_LABELS = {
    "de": "DE",
    "deu": "DE",
    "ger": "DE",
    "en": "EN",
    "eng": "EN",
    "ja": "JA",
    "jpn": "JA",
    "fr": "FR",
    "fra": "FR",
    "fre": "FR",
    "es": "ES",
    "spa": "ES",
    "it": "IT",
    "ita": "IT",
    "ko": "KO",
    "kor": "KO",
    "zh": "ZH",
    "zho": "ZH",
    "chi": "ZH",
    "ru": "RU",
    "rus": "RU",
    "pt": "PT",
    "por": "PT",
    "nl": "NL",
    "nld": "NL",
    "dut": "NL",
    "und": "UND",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _int(value: Any) -> int | None:
    try:
        if value in (None, "", "N/A"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        if value in (None, "", "N/A"):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _ratio(value: Any) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        numerator, denominator = str(value).split("/", 1)
        denominator_value = float(denominator)
        if denominator_value == 0:
            return None
        result = float(numerator) / denominator_value
        return round(result, 3) if math.isfinite(result) else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _codec_label(codec_name: Any, profile: Any = "") -> str:
    codec = str(codec_name or "").strip().lower()
    profile_text = str(profile or "").strip().casefold()
    if codec == "dts":
        if "master audio" in profile_text or "dts-hd ma" in profile_text:
            return "DTS-HD MA"
        if "high resolution" in profile_text or "dts-hd hra" in profile_text:
            return "DTS-HD HRA"
    return _CODEC_LABELS.get(codec, codec.upper() if codec else "Unbekannt")


def _language(value: Any) -> str:
    raw = str(value or "und").strip().lower()
    return _LANGUAGE_LABELS.get(raw, raw.upper()[:5] if raw else "UND")


def _channel_label(channels: int | None, layout: str) -> str:
    normalized = layout.casefold().replace("(side)", "").replace("(back)", "")
    common = {
        "mono": "1.0",
        "stereo": "2.0",
        "2.1": "2.1",
        "3.0": "3.0",
        "4.0": "4.0",
        "5.0": "5.0",
        "5.1": "5.1",
        "6.1": "6.1",
        "7.1": "7.1",
    }
    if normalized in common:
        return common[normalized]
    return {
        1: "1.0",
        2: "2.0",
        3: "2.1",
        4: "4.0",
        5: "5.0",
        6: "5.1",
        7: "6.1",
        8: "7.1",
    }.get(channels or 0, str(channels) if channels else "")


def _resolution_label(width: int | None, height: int | None) -> str:
    if not width and not height:
        return ""
    effective_height = height or 0
    effective_width = width or 0
    if effective_height >= 4000 or effective_width >= 7000:
        return "4320p"
    if effective_height >= 2000 or effective_width >= 3500:
        return "2160p"
    if effective_height >= 1300 or effective_width >= 2400:
        return "1440p"
    if effective_height >= 1000 or effective_width >= 1800:
        return "1080p"
    if effective_height >= 700 or effective_width >= 1200:
        return "720p"
    if effective_height >= 560:
        return "576p"
    if effective_height >= 460:
        return "480p"
    return f"{effective_height}p" if effective_height else f"{effective_width}px"


def _bit_depth(stream: dict[str, Any]) -> int | None:
    direct = _int(stream.get("bits_per_raw_sample")) or _int(stream.get("bits_per_sample"))
    if direct:
        return direct
    pix_fmt = str(stream.get("pix_fmt") or "")
    match = re.search(r"(?:p|gbrp|yuva)(?P<bits>9|10|12|14|16)(?:le|be)?$", pix_fmt)
    if match:
        return int(match.group("bits"))
    return 8 if pix_fmt else None


def _hdr_label(stream: dict[str, Any]) -> str:
    side_data = stream.get("side_data_list") or []
    side_text = " ".join(
        str(value)
        for entry in side_data
        if isinstance(entry, dict)
        for value in entry.values()
    ).casefold()
    tags = stream.get("tags") or {}
    tag_text = " ".join(str(value) for value in tags.values()).casefold()
    profile = str(stream.get("profile") or "").casefold()
    combined = f"{side_text} {tag_text} {profile}"
    if "dovi" in combined or "dolby vision" in combined:
        return "Dolby Vision"
    if "hdr10+" in combined or "dynamic hdr10+" in combined or "smpte2094" in combined:
        return "HDR10+"
    transfer = str(stream.get("color_transfer") or "").casefold()
    primaries = str(stream.get("color_primaries") or "").casefold()
    if transfer in {"smpte2084", "pq"}:
        return "HDR10"
    if transfer == "arib-std-b67":
        return "HLG"
    if "bt2020" in primaries and transfer:
        return "HDR"
    return ""


def _audio_entry(stream: dict[str, Any]) -> dict[str, Any]:
    tags = stream.get("tags") or {}
    disposition = stream.get("disposition") or {}
    profile = str(stream.get("profile") or "")
    codec = _codec_label(stream.get("codec_name"), profile)
    title = str(tags.get("title") or "").strip()
    atmosphere_text = f"{title} {profile}".casefold()
    atmos = "atmos" in atmosphere_text
    if atmos and "Atmos" not in codec:
        codec = f"{codec} Atmos"
    channels = _int(stream.get("channels"))
    layout = str(stream.get("channel_layout") or "").strip()
    language = _language(tags.get("language"))
    channel_label = _channel_label(channels, layout)
    summary = " ".join(
        value
        for value in (language if language != "UND" else "", codec, channel_label)
        if value
    )
    return {
        "index": _int(stream.get("index")),
        "language": language,
        "language_raw": str(tags.get("language") or "und"),
        "codec": codec,
        "codec_name": str(stream.get("codec_name") or ""),
        "profile": profile,
        "channels": channels,
        "channel_layout": layout,
        "channel_label": channel_label,
        "bit_rate": _int(stream.get("bit_rate")) or _int(tags.get("BPS")) or _int(tags.get("BPS-eng")),
        "default": bool(_int(disposition.get("default")) or 0),
        "forced": bool(_int(disposition.get("forced")) or 0),
        "title": title,
        "atmos": atmos,
        "summary": summary,
    }


def _subtitle_entry(stream: dict[str, Any]) -> dict[str, Any]:
    tags = stream.get("tags") or {}
    disposition = stream.get("disposition") or {}
    language = _language(tags.get("language"))
    codec = _codec_label(stream.get("codec_name"))
    title = str(tags.get("title") or "").strip()
    summary = " ".join(value for value in (language if language != "UND" else "", codec, title) if value)
    return {
        "index": _int(stream.get("index")),
        "language": language,
        "codec": codec,
        "default": bool(_int(disposition.get("default")) or 0),
        "forced": bool(_int(disposition.get("forced")) or 0),
        "title": title,
        "summary": summary,
    }


def _video_entry(stream: dict[str, Any]) -> dict[str, Any]:
    disposition = stream.get("disposition") or {}
    width = _int(stream.get("width"))
    height = _int(stream.get("height"))
    fps = _ratio(stream.get("avg_frame_rate")) or _ratio(stream.get("r_frame_rate"))
    pixel_format = str(stream.get("pix_fmt") or "")
    return {
        "index": _int(stream.get("index")),
        "codec": _codec_label(stream.get("codec_name"), stream.get("profile")),
        "codec_name": str(stream.get("codec_name") or ""),
        "profile": str(stream.get("profile") or ""),
        "width": width,
        "height": height,
        "dimensions": f"{width}×{height}" if width and height else "",
        "resolution": _resolution_label(width, height),
        "pix_fmt": pixel_format,
        "pixel_format": pixel_format,
        "bit_depth": _bit_depth(stream),
        "hdr": _hdr_label(stream),
        "fps": fps,
        "frame_rate": fps,
        "bit_rate": _int(stream.get("bit_rate")),
        "default": bool(_int(disposition.get("default")) or 0),
    }


def technical_tags(info: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    video = info.get("video") or {}
    if video.get("resolution"):
        tags.append(str(video["resolution"]))
    if video.get("hdr"):
        tags.append(str(video["hdr"]))
    audio = list(info.get("audio") or [])
    primary = next((track for track in audio if track.get("default")), audio[0] if audio else None)
    if primary and primary.get("codec"):
        codec = str(primary["codec"])
        # Keep filenames compact. Atmos is useful, but language/channel data stays in the UI.
        tags.append(codec)
    return list(dict.fromkeys(tags))


def stream_summary(info: dict[str, Any], *, include_languages: bool = True, max_audio: int = 3) -> str:
    parts: list[str] = []
    video = info.get("video") or {}
    for value in (video.get("resolution"), video.get("codec"), video.get("hdr")):
        if value and value not in parts:
            parts.append(str(value))
    audio_parts: list[str] = []
    for track in list(info.get("audio") or [])[:max_audio]:
        values: list[str] = []
        if include_languages and track.get("language") and track.get("language") != "UND":
            values.append(str(track["language"]))
        if track.get("codec"):
            values.append(str(track["codec"]))
        if track.get("channel_label"):
            values.append(str(track["channel_label"]))
        if values:
            audio_parts.append(" ".join(values))
    if len(info.get("audio") or []) > max_audio:
        audio_parts.append(f"+{len(info['audio']) - max_audio}")
    parts.extend(audio_parts)
    return " · ".join(parts)


def collection_summary(infos: Iterable[dict[str, Any]]) -> str:
    values = [info for info in infos if info]
    if not values:
        return ""

    def unique(field: str, nested: str = "video", limit: int = 2) -> list[str]:
        result: list[str] = []
        for info in values:
            value = (info.get(nested) or {}).get(field)
            if value and str(value) not in result:
                result.append(str(value))
            if len(result) >= limit:
                break
        return result

    parts: list[str] = []
    resolutions = unique("resolution")
    codecs = unique("codec")
    hdrs = unique("hdr")
    if resolutions:
        parts.append("/".join(resolutions))
    if codecs:
        parts.append("/".join(codecs))
    if hdrs:
        parts.append("/".join(hdrs))

    audio_values: list[str] = []
    for info in values:
        for track in info.get("audio") or []:
            codec = str(track.get("codec") or "")
            channel = str(track.get("channel_label") or "")
            value = " ".join(item for item in (codec, channel) if item)
            if value and value not in audio_values:
                audio_values.append(value)
            if len(audio_values) >= 3:
                break
        if len(audio_values) >= 3:
            break
    if audio_values:
        parts.append(" / ".join(audio_values))
    return " · ".join(parts)


def _bitrate_label(value: int | None) -> str:
    if not value:
        return ""
    return f"{value / 1_000_000:.1f} Mbit/s" if value >= 1_000_000 else f"{value / 1_000:.0f} kbit/s"


def parse_ffprobe_payload(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    streams = payload.get("streams") or []
    videos = [_video_entry(stream) for stream in streams if stream.get("codec_type") == "video"]
    audio = [_audio_entry(stream) for stream in streams if stream.get("codec_type") == "audio"]
    subtitles = [_subtitle_entry(stream) for stream in streams if stream.get("codec_type") == "subtitle"]
    video = next((item for item in videos if item.get("default")), videos[0] if videos else {})
    format_info = payload.get("format") or {}
    duration_seconds = _float(format_info.get("duration"))
    overall_bitrate = _int(format_info.get("bit_rate"))
    info: dict[str, Any] = {
        "path": str(path),
        "probed_at": _now(),
        "format": {
            "name": str(format_info.get("format_name") or ""),
            "duration_seconds": duration_seconds,
            "size_bytes": _int(format_info.get("size")),
            "bit_rate": overall_bitrate,
        },
        "duration_seconds": duration_seconds,
        "overall_bitrate": overall_bitrate,
        "overall_bitrate_label": _bitrate_label(overall_bitrate),
        "video": video,
        "audio": audio,
        "subtitles": subtitles,
    }
    info["technical_tags"] = technical_tags(info)
    info["summary"] = stream_summary(info)
    return info


def probe_file(path: Path, *, ffprobe_path: str = "ffprobe", timeout_seconds: int = 45) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise ProbeError(f"Mediendatei nicht gefunden: {path}")
    executable = shutil.which(ffprobe_path) if not Path(ffprobe_path).is_absolute() else ffprobe_path
    if not executable:
        raise ProbeError("ffprobe ist im Container nicht installiert oder nicht auffindbar.")
    command = [
        str(executable),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe-Zeitüberschreitung nach {timeout_seconds} Sekunden.") from exc
    except OSError as exc:
        raise ProbeError(f"ffprobe konnte nicht gestartet werden: {exc}") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "Unbekannter ffprobe-Fehler").strip()
        raise ProbeError(message[:500])
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe hat ungültige JSON-Daten geliefert.") from exc
    return parse_ffprobe_payload(payload, path)


@dataclass(slots=True)
class MediaProbe:
    settings: Any
    database: Any

    @property
    def available(self) -> bool:
        if not getattr(self.settings, "ffprobe_enabled", True):
            return False
        configured = str(getattr(self.settings, "ffprobe_path", "ffprobe"))
        if Path(configured).is_absolute():
            return Path(configured).is_file()
        return shutil.which(configured) is not None

    def probe(self, path: Path, *, force: bool = False) -> dict[str, Any]:
        path = Path(path).resolve()
        try:
            stat = path.stat()
        except OSError as exc:
            raise ProbeError(f"Mediendatei ist nicht lesbar: {path}") from exc
        if not force:
            cached = self.database.get_media_probe(path, stat.st_size, stat.st_mtime)
            if cached:
                return cached
        try:
            info = probe_file(
                path,
                ffprobe_path=self.settings.ffprobe_path,
                timeout_seconds=self.settings.ffprobe_timeout_seconds,
            )
        except ProbeError as exc:
            self.database.store_media_probe_error(path, stat.st_size, stat.st_mtime, str(exc))
            raise
        self.database.store_media_probe(path, stat.st_size, stat.st_mtime, info)
        return info

    def cached(self, path: Path) -> dict[str, Any] | None:
        path = Path(path).resolve()
        try:
            stat = path.stat()
        except OSError:
            return None
        return self.database.get_media_probe(path, stat.st_size, stat.st_mtime)

    def cached_under(self, folder: Path, *, limit: int = 500) -> list[dict[str, Any]]:
        return self.database.list_media_probes_under(Path(folder).resolve(), limit=limit)

    def relocate(self, source: Path, target: Path) -> None:
        self.database.relocate_media_probe(Path(source).resolve(), Path(target).resolve())

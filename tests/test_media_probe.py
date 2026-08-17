from __future__ import annotations

from pathlib import Path

from app.catalog import aggregate_probes
from app.media_probe import parse_ffprobe_payload


def test_parse_ffprobe_payload_reads_real_video_audio_and_subtitles(tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    payload = {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "hevc",
                "profile": "Main 10",
                "width": 1920,
                "height": 1080,
                "pix_fmt": "yuv420p10le",
                "avg_frame_rate": "24000/1001",
                "color_transfer": "smpte2084",
                "color_primaries": "bt2020",
                "disposition": {"default": 1},
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "eac3",
                "channels": 6,
                "channel_layout": "5.1(side)",
                "bit_rate": "768000",
                "tags": {"language": "deu", "title": "Deutsch"},
                "disposition": {"default": 1, "forced": 0},
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "channel_layout": "stereo",
                "tags": {"language": "eng", "title": "English"},
                "disposition": {"default": 0, "forced": 0},
            },
            {
                "index": 3,
                "codec_type": "subtitle",
                "codec_name": "subrip",
                "tags": {"language": "deu", "title": "Deutsch erzwungen"},
                "disposition": {"default": 0, "forced": 1},
            },
        ],
        "format": {
            "format_name": "matroska,webm",
            "duration": "7200.25",
            "size": "123456789",
            "bit_rate": "12000000",
        },
    }

    info = parse_ffprobe_payload(payload, path)

    assert info["video"]["resolution"] == "1080p"
    assert info["video"]["codec"] == "HEVC"
    assert info["video"]["bit_depth"] == 10
    assert info["video"]["hdr"] == "HDR10"
    assert info["video"]["frame_rate"] == 23.976
    assert info["audio"][0]["language"] == "DE"
    assert info["audio"][0]["codec"] == "EAC3"
    assert info["audio"][0]["channel_label"] == "5.1"
    assert info["audio"][0]["default"] is True
    assert info["audio"][1]["language"] == "EN"
    assert info["audio"][1]["channel_label"] == "2.0"
    assert info["subtitles"][0]["language"] == "DE"
    assert info["subtitles"][0]["forced"] is True
    assert info["technical_tags"] == ["1080p", "HDR10", "EAC3"]
    assert "DE EAC3 5.1" in info["summary"]
    assert info["overall_bitrate_label"] == "12.0 Mbit/s"


def test_catalog_aggregate_combines_stream_profiles(tmp_path: Path) -> None:
    first = parse_ffprobe_payload(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "disposition": {"default": 1},
                },
                {
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "channels": 6,
                    "channel_layout": "5.1",
                    "tags": {"language": "deu"},
                    "disposition": {"default": 1},
                },
            ],
            "format": {},
        },
        tmp_path / "one.mkv",
    )
    second = parse_ffprobe_payload(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 3840,
                    "height": 2160,
                    "color_transfer": "smpte2084",
                    "disposition": {"default": 1},
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "tags": {"language": "eng"},
                    "disposition": {"default": 1},
                },
            ],
            "format": {},
        },
        tmp_path / "two.mkv",
    )

    aggregate = aggregate_probes([first, second])

    assert aggregate.probed_files == 2
    assert set(aggregate.resolutions) == {"1080p", "2160p"}
    assert set(aggregate.video_codecs) == {"H.264", "HEVC"}
    assert aggregate.hdr_formats == ("HDR10",)
    assert set(aggregate.audio_languages) == {"DE", "EN"}
    assert set(aggregate.audio_codecs) == {"AAC", "AC3"}
    assert set(aggregate.channel_layouts) == {"2.0", "5.1"}


def test_probe_errors_can_be_listed(tmp_path) -> None:
    from app.db import Database

    database = Database(tmp_path / "probe-errors.sqlite3")
    database.initialize()
    path = tmp_path / "broken.mkv"
    path.write_bytes(b"broken")
    stat = path.stat()
    database.store_media_probe_error(path.resolve(), stat.st_size, stat.st_mtime, "Invalid data")
    errors = database.list_media_probe_errors()
    assert len(errors) == 1
    assert errors[0]["file_path"] == str(path.resolve())
    assert errors[0]["error"] == "Invalid data"

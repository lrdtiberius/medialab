from __future__ import annotations

from pathlib import Path

from app.catalog import MediaCatalog
from app.config import load_settings


def _write_nfo(path: Path, *, root: str, title: str, year: int, tmdb_id: int) -> None:
    path.write_text(
        f"""<{root}>
  <title>{title}</title>
  <originaltitle>{title} Original</originaltitle>
  <year>{year}</year>
  <plot>Lokale Beschreibung</plot>
  <rating>7.5</rating>
  <genre>Drama</genre>
  <uniqueid type=\"tmdb\">{tmdb_id}</uniqueid>
  <uniqueid type=\"imdb\">tt000{tmdb_id}</uniqueid>
</{root}>
""",
        encoding="utf-8",
    )


def test_catalog_indexes_all_three_libraries(tmp_path, monkeypatch) -> None:
    media = tmp_path / "video"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    settings = load_settings()

    movie = settings.movie_root / "Testfilm (2020)"
    series = settings.tv_root / "Testserie (2021)"
    anime = settings.anime_root / "Animefilm (2022)"
    for folder in (movie, series, anime):
        folder.mkdir(parents=True)

    _write_nfo(movie / "movie.nfo", root="movie", title="Testfilm", year=2020, tmdb_id=1)
    _write_nfo(series / "tvshow.nfo", root="tvshow", title="Testserie", year=2021, tmdb_id=2)
    _write_nfo(anime / "movie.nfo", root="movie", title="Animefilm", year=2022, tmdb_id=3)
    (movie / "poster.jpg").write_bytes(b"poster")

    catalog = MediaCatalog(settings, ttl_seconds=999)
    items = catalog.refresh(force=True)

    assert len(items) == 3
    assert catalog.counts() == {"all": 3, "movies": 1, "series": 1, "anime": 1}
    movie_item = next(item for item in items if item.library == "movies")
    assert movie_item.title == "Testfilm"
    assert movie_item.tmdb_id == 1
    assert movie_item.has_nfo is True
    assert movie_item.has_poster is True


def test_catalog_details_count_series_files(tmp_path, monkeypatch) -> None:
    media = tmp_path / "video"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    settings = load_settings()

    series = settings.tv_root / "Testserie (2021)"
    season = series / "Season 1"
    season.mkdir(parents=True)
    _write_nfo(series / "tvshow.nfo", root="tvshow", title="Testserie", year=2021, tmdb_id=2)
    (season / "Testserie - S01E01.mkv").write_bytes(b"video")
    (season / "Testserie - S01E01.deu.srt").write_text("subtitle", encoding="utf-8")
    (season / "Testserie - S01E01.nfo").write_text("<episodedetails />", encoding="utf-8")
    (series / "poster.jpg").write_bytes(b"poster")

    catalog = MediaCatalog(settings, ttl_seconds=999)
    item = catalog.refresh(force=True)[0]
    details = catalog.details(item.item_id)

    assert details is not None
    assert details.video_count == 1
    assert details.episode_count == 1
    assert details.season_count == 1
    assert details.subtitle_count == 1
    assert details.nfo_count == 2
    assert details.artwork_count == 1
    assert details.files_truncated is False


def test_catalog_artwork_is_restricted_to_item_folder(tmp_path, monkeypatch) -> None:
    media = tmp_path / "video"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    settings = load_settings()
    movie = settings.movie_root / "Testfilm (2020)"
    movie.mkdir(parents=True)
    (movie / "poster.jpg").write_bytes(b"poster")

    catalog = MediaCatalog(settings, ttl_seconds=999)
    item = catalog.refresh(force=True)[0]

    assert catalog.artwork(item.item_id, "poster") == (movie / "poster.jpg").resolve()
    assert catalog.artwork(item.item_id, "backdrop") is None


def test_catalog_uses_cached_ffprobe_data_for_overview_and_files(tmp_path, monkeypatch) -> None:
    from app.db import Database

    media = tmp_path / "video-tech"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data-tech"))
    settings = load_settings()

    movie = settings.movie_root / "Technikfilm (2024)"
    movie.mkdir(parents=True)
    _write_nfo(movie / "movie.nfo", root="movie", title="Technikfilm", year=2024, tmdb_id=44)
    video = movie / "Technikfilm (2024).mkv"
    video.write_bytes(b"video")
    stat = video.stat()

    payload = {
        "summary": "1080p · HEVC · DE EAC3 5.1",
        "video": {"resolution": "1080p", "codec": "HEVC", "hdr": ""},
        "audio": [
            {
                "language": "DE",
                "codec": "EAC3",
                "channel_label": "5.1",
                "summary": "DE EAC3 5.1",
            }
        ],
        "subtitles": [],
    }
    database = Database(settings.database_path)
    database.initialize()
    database.store_media_probe(video.resolve(), stat.st_size, stat.st_mtime, payload)

    catalog = MediaCatalog(settings, database=database, ttl_seconds=999)
    item = catalog.refresh(force=True)[0]
    assert item.technical.probed_files == 1
    assert item.technical.summary == "1080p · HEVC · DE EAC3 5.1"

    details = catalog.details(item.item_id)
    assert details is not None
    assert details.unprobed_video_count == 0
    video_row = next(row for row in details.files if row.kind == "video")
    assert video_row.technical_summary == "1080p · HEVC · DE EAC3 5.1"
    assert video_row.technical_info["audio"][0]["language"] == "DE"

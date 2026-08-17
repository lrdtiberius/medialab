from __future__ import annotations

from app.config import load_settings
from app.db import Database
from app.scanner import InboxScanner


class DummyService:
    pass


def test_anime_episode_from_animes_inbox_is_normalized_to_series(tmp_path, monkeypatch) -> None:
    media = tmp_path / "Video"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    settings = load_settings()
    source_dir = settings.inbox_root / "Animes"
    source_dir.mkdir(parents=True)
    source = source_dir / "[Group] Frieren - 01 [1080p].mkv"
    source.write_bytes(b"video")

    database = Database(settings.database_path)
    database.initialize()
    scanner = InboxScanner(settings, database, DummyService())
    scanner._discover_sync()

    job = database.get_job_by_path(source.resolve())
    assert job is not None
    assert job["media_type"] == "tv"
    assert job["source_hint"] == "series"
    assert job["library"] == "series"
    assert job["season"] == 1
    assert job["episode"] == 1


def test_anime_movie_from_animes_inbox_keeps_anime_hint(tmp_path, monkeypatch) -> None:
    media = tmp_path / "Video2"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data2"))
    settings = load_settings()
    source_dir = settings.inbox_root / "Animes"
    source_dir.mkdir(parents=True)
    source = source_dir / "Your.Name.2016.1080p.mkv"
    source.write_bytes(b"video")

    database = Database(settings.database_path)
    database.initialize()
    scanner = InboxScanner(settings, database, DummyService())
    scanner._discover_sync()

    job = database.get_job_by_path(source.resolve())
    assert job is not None
    assert job["media_type"] == "movie"
    assert job["source_hint"] == "anime"
    assert job["library"] == "anime"



def test_config_directory_is_ignored_by_scanner(tmp_path, monkeypatch) -> None:
    media = tmp_path / "Videos"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data-config-ignore"))
    settings = load_settings()
    config_dir = settings.inbox_root / "_config"
    config_dir.mkdir(parents=True)
    # Even a file with a video extension must never be treated as media here.
    fake_video = config_dir / "secret.mkv"
    fake_video.write_bytes(b"not a real video")

    database = Database(settings.database_path)
    database.initialize()
    scanner = InboxScanner(settings, database, DummyService())
    scanner._discover_sync()

    assert database.get_job_by_path(fake_video.resolve()) is None


def test_renamed_inbox_file_removes_stale_overview_row(tmp_path, monkeypatch) -> None:
    media = tmp_path / "Video-rename"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data-rename"))
    settings = load_settings()
    settings.inbox_root.mkdir(parents=True)

    original = settings.inbox_root / "Ame & Yuki - Die Wolfskinder (2012) 1080p EAC3.mp4"
    original.write_bytes(b"video")

    database = Database(settings.database_path)
    database.initialize()
    scanner = InboxScanner(settings, database, DummyService())
    scanner._discover_sync()

    old_job = database.get_job_by_path(original.resolve())
    assert old_job is not None
    database.update_job(old_job["id"], status="ignored")

    renamed = settings.inbox_root / "Ame & Yuki - Die Wolfskinder.mp4"
    original.rename(renamed)
    scanner._discover_sync()

    assert database.get_job_by_path(original.resolve()) is None
    new_job = database.get_job_by_path(renamed.resolve())
    assert new_job is not None
    assert new_job["parsed_title"] == "Ame & Yuki - Die Wolfskinder"
    assert scanner.last_pruned_count == 1


def test_manually_renamed_file_replaces_stale_overview_row(tmp_path, monkeypatch) -> None:
    media = tmp_path / "Video-rename"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data-rename"))
    settings = load_settings()
    settings.inbox_root.mkdir(parents=True)
    original = settings.inbox_root / "Old Name (2020).mkv"
    original.write_bytes(b"video")

    database = Database(settings.database_path)
    database.initialize()
    scanner = InboxScanner(settings, database, DummyService())
    scanner._discover_sync()
    old_job = database.get_job_by_path(original.resolve())
    assert old_job is not None
    database.update_job(old_job["id"], status="ignored")

    renamed = settings.inbox_root / "New Name (2020).mkv"
    original.rename(renamed)
    scanner._discover_sync()

    assert database.get_job_by_path(original.resolve()) is None
    assert database.get_job_by_path(renamed.resolve()) is not None
    assert scanner.last_pruned_count == 1


def test_inbox_waits_for_multiple_stable_checks_before_ready(tmp_path, monkeypatch) -> None:
    import os
    import time
    from datetime import UTC, datetime, timedelta

    media = tmp_path / "Video-stability"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data-stability"))
    monkeypatch.setenv("FILE_STABLE_SECONDS", "20")
    monkeypatch.setenv("FILE_STABLE_MIN_CHECKS", "3")
    monkeypatch.setenv("FILE_STABLE_MTIME_SECONDS", "10")
    settings = load_settings()
    settings.inbox_root.mkdir(parents=True)
    source = settings.inbox_root / "Big 4K Movie (2025).mkv"
    source.write_bytes(b"0123456789")
    old = time.time() - 120
    os.utime(source, (old, old))

    database = Database(settings.database_path)
    database.initialize()
    scanner = InboxScanner(settings, database, DummyService())

    scanner._discover_sync()
    job = database.get_job_by_path(source.resolve())
    assert job is not None
    assert job["status"] == "waiting"

    # Simulate that the first unchanged observation already happened long ago.
    with database._connect() as connection:
        connection.execute(
            "UPDATE jobs SET stable_since = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=60)).isoformat(timespec="seconds"), job["id"]),
        )

    scanner._discover_sync()
    assert database.get_job(job["id"])["status"] == "waiting"
    scanner._discover_sync()
    assert database.get_job(job["id"])["status"] == "pending"


def test_inbox_change_resets_stable_check_counter(tmp_path, monkeypatch) -> None:
    import os
    import time

    media = tmp_path / "Video-growing"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data-growing"))
    monkeypatch.setenv("FILE_STABLE_SECONDS", "20")
    monkeypatch.setenv("FILE_STABLE_MIN_CHECKS", "3")
    monkeypatch.setenv("FILE_STABLE_MTIME_SECONDS", "10")
    settings = load_settings()
    settings.inbox_root.mkdir(parents=True)
    source = settings.inbox_root / "Growing Movie (2025).mkv"
    source.write_bytes(b"abc")
    old = time.time() - 120
    os.utime(source, (old, old))

    database = Database(settings.database_path)
    database.initialize()
    scanner = InboxScanner(settings, database, DummyService())
    scanner._discover_sync()
    scanner._discover_sync()
    source.write_bytes(b"abcdefghi")
    os.utime(source, None)
    scanner._discover_sync()

    job = database.get_job_by_path(source.resolve())
    assert job is not None
    assert job["status"] == "waiting"
    assert scanner._stability[str(source.resolve())][2] == 1

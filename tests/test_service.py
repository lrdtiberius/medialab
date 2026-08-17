from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.service import MediaService


def _service() -> MediaService:
    service = MediaService.__new__(MediaService)
    service.settings = SimpleNamespace(
        anime_auto_detect=True,
        anime_languages=("ja",),
    )
    return service


def test_movie_targets_are_limited_to_films_or_animes() -> None:
    MediaService.validate_library("movie", "movies")
    MediaService.validate_library("movie", "anime")
    with pytest.raises(ValueError):
        MediaService.validate_library("movie", "series")


def test_tv_target_is_always_series() -> None:
    MediaService.validate_library("tv", "series")
    with pytest.raises(ValueError):
        MediaService.validate_library("tv", "anime")
    with pytest.raises(ValueError):
        MediaService.validate_library("tv", "movies")


def test_anime_tv_is_routed_to_series_even_with_anime_hint() -> None:
    item = {"genre_ids": [16], "original_language": "ja"}
    assert _service().choose_library("tv", item, "anime") == "series"


def test_anime_movie_is_routed_to_animes() -> None:
    item = {"genre_ids": [16], "original_language": "ja"}
    assert _service().choose_library("movie", item, None) == "anime"


def test_normal_movie_is_routed_to_films() -> None:
    item = {"genre_ids": [28], "original_language": "en"}
    assert _service().choose_library("movie", item, None) == "movies"


class _RetryDatabase:
    def __init__(self, job):
        self.job = dict(job)
        self.updated = {}
        self.reset = False

    def get_job(self, job_id):
        return dict(self.job) if job_id == self.job["id"] else None

    def update_job(self, job_id, **fields):
        assert job_id == self.job["id"]
        self.updated.update(fields)
        self.job.update(fields)

    def reset_job(self, job_id):
        assert job_id == self.job["id"]
        self.reset = True


def test_retry_reparses_existing_movie_job() -> None:
    database = _RetryDatabase(
        {
            "id": 7,
            "source_path": "/media/New/8 Mile (2002) 1080p AAC.mp4",
            "source_hint": None,
        }
    )
    service = MediaService.__new__(MediaService)
    service.database = database

    service.retry(7)

    assert database.updated["parsed_title"] == "8 Mile"
    assert database.updated["parsed_year"] == 2002
    assert database.updated["technical_tags"] == ["1080p", "AAC"]
    assert database.updated["search_query"] == "8 Mile"
    assert database.reset is True


def test_retry_keeps_tv_target_in_series() -> None:
    database = _RetryDatabase(
        {
            "id": 8,
            "source_path": "/media/New/Animes/Frieren.S01E02.1080p.mkv",
            "source_hint": "series",
        }
    )
    service = MediaService.__new__(MediaService)
    service.database = database

    service.retry(8)

    assert database.updated["media_type"] == "tv"
    assert database.updated["library"] == "series"
    assert database.updated["season"] == 1
    assert database.updated["episode"] == 2

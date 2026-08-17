from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import load_settings
from app.library import LibraryIndex
from app.processor import MediaProcessor


class FakeTMDb:
    async def movie_details(self, tmdb_id: int):
        return {
            "id": tmdb_id,
            "title": "2 Fast 2 Furious",
            "original_title": "2 Fast 2 Furious",
            "release_date": "2003-06-05",
            "overview": "Test",
            "genres": [{"id": 28, "name": "Action"}],
            "production_countries": [],
            "production_companies": [],
            "credits": {"cast": [], "crew": []},
            "external_ids": {"imdb_id": "tt0322259"},
            "images": {},
        }

    async def tv_details(self, tmdb_id: int):
        return {
            "id": tmdb_id,
            "name": "A Couple of Cuckoos",
            "original_name": "Kakkou no Iinazuke",
            "first_air_date": "2022-04-24",
            "overview": "Test",
            "genres": [{"id": 16, "name": "Animation"}],
            "networks": [],
            "origin_country": ["JP"],
            "credits": {"cast": [], "crew": []},
            "external_ids": {"tvdb_id": 401840},
            "images": {},
        }

    async def season_details(self, tmdb_id: int, season: int):
        return {"id": 10, "season_number": season, "poster_path": None}

    async def episode_details(self, tmdb_id: int, season: int, episode: int):
        return {
            "id": 100 + episode,
            "name": "Sei mein fester Freund!",
            "season_number": season,
            "episode_number": episode,
            "air_date": "2022-04-24",
            "overview": "Testfolge",
            "credits": {"cast": [], "crew": []},
            "external_ids": {},
            "still_path": None,
        }


async def _movie_case(tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "Videos"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DOWNLOAD_ARTWORK", "false")
    settings = load_settings()
    settings.inbox_root.mkdir(parents=True)
    source = settings.inbox_root / "2.Fast.2.Furious.2003.720p.EAC3.mp4"
    source.write_bytes(b"video")
    subtitle = source.with_name(source.stem + ".deu.srt")
    subtitle.write_text("subtitle")
    processor = MediaProcessor(settings, FakeTMDb(), LibraryIndex(settings))
    result = await processor.process(
        {
            "id": 1,
            "source_path": str(source),
            "parsed_title": "2 Fast 2 Furious",
            "technical_tags": ["720p", "EAC3"],
        },
        tmdb_id=1,
        media_type="movie",
        library="movies",
    )
    assert result.target_path.exists()
    assert result.target_path.name == "2 Fast 2 Furious (2003) 720p EAC3.mp4"
    assert result.target_path.with_name(result.target_path.stem + ".deu.srt").exists()
    assert (result.target_path.parent / "movie.nfo").exists()


async def _episode_case(tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "Videos2"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data2"))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DOWNLOAD_ARTWORK", "false")
    settings = load_settings()
    settings.inbox_root.mkdir(parents=True)
    source = settings.inbox_root / "A.Couple.of.Cuckoos.S01E01.1080p.mp4"
    source.write_bytes(b"video")
    processor = MediaProcessor(settings, FakeTMDb(), LibraryIndex(settings))
    result = await processor.process(
        {
            "id": 2,
            "source_path": str(source),
            "parsed_title": "A Couple of Cuckoos",
            "technical_tags": ["1080p"],
        },
        tmdb_id=2,
        media_type="tv",
        library="series",
        season=1,
        episode=1,
    )
    assert result.target_path.exists()
    assert result.target_path.name == "A Couple of Cuckoos - S01E01 - Sei mein fester Freund!.mp4"
    assert (result.target_path.parents[1] / "tvshow.nfo").exists()
    assert result.target_path.with_suffix(".nfo").exists()


def test_movie_processing(tmp_path, monkeypatch) -> None:
    asyncio.run(_movie_case(tmp_path, monkeypatch))


def test_episode_processing(tmp_path, monkeypatch) -> None:
    asyncio.run(_episode_case(tmp_path, monkeypatch))

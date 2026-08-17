from pathlib import Path

from app.parser import parse_media_filename, sanitize_component


def test_movie_filename_parsing() -> None:
    parsed = parse_media_filename("2.Fast.2.Furious.2003.720p.EAC3.mkv")
    assert parsed.media_type == "movie"
    assert parsed.title == "2 Fast 2 Furious"
    assert parsed.year == 2003
    assert parsed.technical_tags == ["720p", "EAC3"]


def test_tv_filename_parsing() -> None:
    parsed = parse_media_filename("A.Couple.of.Cuckoos.S01E02.1080p.mkv")
    assert parsed.media_type == "tv"
    assert parsed.title == "A Couple of Cuckoos"
    assert parsed.season == 1
    assert parsed.episode == 2


def test_anime_absolute_episode_parsing() -> None:
    parsed = parse_media_filename(
        "[SubsPlease] Frieren - 01 (1080p) [ABC12345].mkv",
        anime_hint=True,
    )
    assert parsed.media_type == "tv"
    assert parsed.title == "Frieren"
    assert parsed.season == 1
    assert parsed.episode == 1


def test_sanitize_component() -> None:
    assert sanitize_component('A/B: C? "D"') == "A - B - C D"


def test_movie_year_in_parentheses_does_not_leave_opening_bracket() -> None:
    parsed = parse_media_filename("8 Mile (2002) 1080p AAC.mp4")
    assert parsed.media_type == "movie"
    assert parsed.title == "8 Mile"
    assert parsed.year == 2002
    assert parsed.technical_tags == ["1080p", "AAC"]

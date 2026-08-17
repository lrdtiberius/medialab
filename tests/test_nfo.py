import xml.etree.ElementTree as ET

from app.nfo import write_movie_nfo, write_tvshow_nfo


def test_movie_nfo_contains_tmdb_id(tmp_path) -> None:
    destination = tmp_path / "movie.nfo"
    write_movie_nfo(
        {
            "id": 123,
            "title": "Testfilm",
            "original_title": "Test Movie",
            "release_date": "2020-01-02",
            "overview": "Handlung",
            "vote_average": 7.5,
            "vote_count": 42,
            "genres": [{"id": 1, "name": "Drama"}],
            "external_ids": {"imdb_id": "tt123"},
        },
        destination,
    )
    root = ET.parse(destination).getroot()
    assert root.findtext("title") == "Testfilm"
    assert root.findtext("uniqueid[@type='tmdb']") == "123"
    assert root.findtext("uniqueid[@type='imdb']") == "tt123"


def test_tvshow_nfo_contains_title(tmp_path) -> None:
    destination = tmp_path / "tvshow.nfo"
    write_tvshow_nfo(
        {
            "id": 77,
            "name": "Testserie",
            "original_name": "Test Show",
            "first_air_date": "2022-04-01",
            "genres": [],
        },
        destination,
    )
    root = ET.parse(destination).getroot()
    assert root.findtext("title") == "Testserie"
    assert root.findtext("year") == "2022"

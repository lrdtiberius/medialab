from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


def _text(parent: ET.Element, tag: str, value: Any) -> ET.Element | None:
    if value is None or value == "":
        return None
    node = ET.SubElement(parent, tag)
    node.text = str(value)
    return node


def _many(parent: ET.Element, tag: str, values: Iterable[Any]) -> None:
    for value in values:
        if isinstance(value, dict):
            value = value.get("name")
        _text(parent, tag, value)


def _ratings(parent: ET.Element, vote_average: Any, vote_count: Any) -> None:
    if vote_average in (None, ""):
        return
    ratings = ET.SubElement(parent, "ratings")
    rating = ET.SubElement(ratings, "rating", {"name": "themoviedb", "max": "10", "default": "true"})
    _text(rating, "value", vote_average)
    _text(rating, "votes", vote_count or 0)


def _unique_ids(parent: ET.Element, tmdb_id: Any, external_ids: dict[str, Any] | None) -> None:
    if tmdb_id is not None:
        node = ET.SubElement(parent, "uniqueid", {"type": "tmdb", "default": "true"})
        node.text = str(tmdb_id)
        _text(parent, "tmdbid", tmdb_id)
    external_ids = external_ids or {}
    mappings = (
        ("imdb_id", "imdb"),
        ("tvdb_id", "tvdb"),
    )
    for source, kind in mappings:
        value = external_ids.get(source)
        if value:
            node = ET.SubElement(parent, "uniqueid", {"type": kind, "default": "false"})
            node.text = str(value)


def _cast(parent: ET.Element, credits: dict[str, Any] | None, limit: int = 30) -> None:
    for person in (credits or {}).get("cast", [])[:limit]:
        actor = ET.SubElement(parent, "actor")
        _text(actor, "name", person.get("name"))
        _text(actor, "role", person.get("character"))
        _text(actor, "order", person.get("order"))
        if person.get("profile_path"):
            _text(actor, "thumb", f"https://image.tmdb.org/t/p/h632{person['profile_path']}")


def _crew(parent: ET.Element, credits: dict[str, Any] | None) -> None:
    credits = credits or {}
    directors: list[str] = []
    writers: list[str] = []
    for person in credits.get("crew", []):
        job = (person.get("job") or "").lower()
        department = (person.get("department") or "").lower()
        name = person.get("name")
        if not name:
            continue
        if job == "director" and name not in directors:
            directors.append(name)
        if job in {"writer", "screenplay", "story", "teleplay"} or department == "writing":
            if name not in writers:
                writers.append(name)
    _many(parent, "director", directors)
    _many(parent, "credits", writers)


def _certification_movie(details: dict[str, Any], region: str) -> str | None:
    results = (details.get("release_dates") or {}).get("results", [])
    for country in results:
        if country.get("iso_3166_1") != region:
            continue
        certifications = [
            item.get("certification")
            for item in country.get("release_dates", [])
            if item.get("certification")
        ]
        if certifications:
            return certifications[0]
    return None


def _certification_tv(details: dict[str, Any], region: str) -> str | None:
    for item in (details.get("content_ratings") or {}).get("results", []):
        if item.get("iso_3166_1") == region and item.get("rating"):
            return item["rating"]
    return None


def _write(root: ET.Element, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(destination, encoding="utf-8", xml_declaration=True)


def write_movie_nfo(details: dict[str, Any], destination: Path, region: str = "DE") -> None:
    root = ET.Element("movie")
    title = details.get("title") or details.get("original_title")
    _text(root, "title", title)
    _text(root, "originaltitle", details.get("original_title"))
    release_date = details.get("release_date") or ""
    _text(root, "year", release_date[:4] if len(release_date) >= 4 else None)
    _text(root, "premiered", release_date)
    _text(root, "plot", details.get("overview"))
    _text(root, "outline", details.get("overview"))
    _text(root, "tagline", details.get("tagline"))
    _text(root, "runtime", details.get("runtime"))
    _text(root, "mpaa", _certification_movie(details, region))
    _text(root, "status", details.get("status"))
    _text(root, "homepage", details.get("homepage"))
    _ratings(root, details.get("vote_average"), details.get("vote_count"))
    _unique_ids(root, details.get("id"), details.get("external_ids"))
    _many(root, "genre", details.get("genres") or [])
    _many(root, "country", details.get("production_countries") or [])
    _many(root, "studio", details.get("production_companies") or [])
    collection = details.get("belongs_to_collection")
    if collection:
        set_node = ET.SubElement(root, "set")
        _text(set_node, "name", collection.get("name"))
        _text(set_node, "overview", collection.get("overview"))
    _crew(root, details.get("credits"))
    _cast(root, details.get("credits"))
    _write(root, destination)


def write_tvshow_nfo(details: dict[str, Any], destination: Path, region: str = "DE") -> None:
    root = ET.Element("tvshow")
    title = details.get("name") or details.get("original_name")
    _text(root, "title", title)
    _text(root, "originaltitle", details.get("original_name"))
    first_air = details.get("first_air_date") or ""
    _text(root, "year", first_air[:4] if len(first_air) >= 4 else None)
    _text(root, "premiered", first_air)
    _text(root, "plot", details.get("overview"))
    _text(root, "outline", details.get("overview"))
    _text(root, "tagline", details.get("tagline"))
    runtimes = details.get("episode_run_time") or []
    _text(root, "runtime", runtimes[0] if runtimes else None)
    _text(root, "status", details.get("status"))
    _text(root, "mpaa", _certification_tv(details, region))
    _text(root, "homepage", details.get("homepage"))
    _ratings(root, details.get("vote_average"), details.get("vote_count"))
    _unique_ids(root, details.get("id"), details.get("external_ids"))
    _many(root, "genre", details.get("genres") or [])
    _many(root, "studio", details.get("networks") or [])
    _many(root, "country", details.get("origin_country") or [])
    _cast(root, details.get("credits"))
    _write(root, destination)


def write_episode_nfo(
    series_details: dict[str, Any],
    episode_details: list[dict[str, Any]],
    destination: Path,
) -> None:
    first = episode_details[0]
    root = ET.Element("episodedetails")
    names = [item.get("name") or f"Episode {item.get('episode_number')}" for item in episode_details]
    plots = [item.get("overview") for item in episode_details if item.get("overview")]
    _text(root, "title", " + ".join(names))
    _text(root, "showtitle", series_details.get("name") or series_details.get("original_name"))
    _text(root, "season", first.get("season_number"))
    _text(root, "episode", first.get("episode_number"))
    _text(root, "aired", first.get("air_date"))
    _text(root, "plot", "\n\n".join(plots))
    _text(root, "runtime", first.get("runtime"))
    _ratings(root, first.get("vote_average"), first.get("vote_count"))
    _unique_ids(root, first.get("id"), first.get("external_ids"))
    _crew(root, first.get("credits"))
    _cast(root, first.get("credits"), limit=20)
    _write(root, destination)

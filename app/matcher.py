from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from .parser import normalize_for_match


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: dict[str, Any]
    score: float
    title_score: float
    year_delta: int | None


def _candidate_titles(candidate: dict[str, Any]) -> list[str]:
    names = [
        candidate.get("title"),
        candidate.get("name"),
        candidate.get("original_title"),
        candidate.get("original_name"),
    ]
    return [str(value) for value in names if value]


def _title_similarity(query: str, candidate: dict[str, Any]) -> float:
    normalized_query = normalize_for_match(query)
    if not normalized_query:
        return 0.0
    best = 0.0
    for title in _candidate_titles(candidate):
        normalized_title = normalize_for_match(title)
        if not normalized_title:
            continue
        ratio = fuzz.ratio(normalized_query, normalized_title) / 100.0
        token = fuzz.token_set_ratio(normalized_query, normalized_title) / 100.0
        partial = fuzz.partial_ratio(normalized_query, normalized_title) / 100.0
        # Exact and token-order independent matches should dominate. Partial matches
        # help with release suffixes but are deliberately down-weighted.
        score = max(ratio, token, partial * 0.92)
        best = max(best, score)
    return best


def rank_candidate(
    query_title: str,
    query_year: int | None,
    candidate: dict[str, Any],
) -> RankedCandidate:
    title_score = _title_similarity(query_title, candidate)
    candidate_year = candidate.get("year")
    year_delta: int | None = None
    year_adjustment = 0.0
    if query_year and candidate_year:
        year_delta = abs(int(query_year) - int(candidate_year))
        if year_delta == 0:
            year_adjustment = 0.09
        elif year_delta == 1:
            year_adjustment = 0.04
        elif year_delta <= 3:
            year_adjustment = -0.04
        else:
            year_adjustment = -0.12

    popularity = float(candidate.get("popularity") or 0.0)
    popularity_bonus = min(0.025, popularity / 10_000.0)
    score = max(0.0, min(1.0, title_score * 0.90 + year_adjustment + popularity_bonus))
    return RankedCandidate(
        candidate=candidate,
        score=round(score, 4),
        title_score=round(title_score, 4),
        year_delta=year_delta,
    )


def rank_candidates(
    query_title: str,
    query_year: int | None,
    candidates: list[dict[str, Any]],
) -> list[RankedCandidate]:
    ranked = [rank_candidate(query_title, query_year, candidate) for candidate in candidates]
    return sorted(ranked, key=lambda item: item.score, reverse=True)


def is_confident_match(
    ranked: list[RankedCandidate],
    threshold: float,
    margin: float,
    *,
    year_was_present: bool,
) -> bool:
    if not ranked:
        return False
    effective_threshold = threshold if year_was_present else min(0.99, threshold + 0.03)
    if ranked[0].score < effective_threshold:
        return False
    if len(ranked) == 1:
        return True
    return (ranked[0].score - ranked[1].score) >= margin

from app.matcher import is_confident_match, rank_candidates


def test_exact_title_and_year_wins() -> None:
    candidates = [
        {"id": 1, "title": "The Thing", "original_title": "The Thing", "year": 1982, "popularity": 30},
        {"id": 2, "title": "The Thing", "original_title": "The Thing", "year": 2011, "popularity": 50},
    ]
    ranked = rank_candidates("The Thing", 1982, candidates)
    assert ranked[0].candidate["id"] == 1
    assert is_confident_match(ranked, 0.92, 0.08, year_was_present=True)


def test_ambiguous_without_year_is_not_confident() -> None:
    candidates = [
        {"id": 1, "title": "The Thing", "year": 1982, "popularity": 30},
        {"id": 2, "title": "The Thing", "year": 2011, "popularity": 30},
    ]
    ranked = rank_candidates("The Thing", None, candidates)
    assert not is_confident_match(ranked, 0.92, 0.08, year_was_present=False)

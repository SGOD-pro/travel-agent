"""Unit tests for PlaceExtractionService and multi-site consensus ranking."""

from __future__ import annotations

from unittest.mock import MagicMock

from functions.orchestrator.evidence.place_extraction import PlaceExtractionService


def test_extract_candidates_from_serpapi_local_results() -> None:
    obs1 = MagicMock()
    obs1.publisher = "tripadvisor.com"
    obs1.source_url = "https://tripadvisor.com/kolkata-flurys"
    obs1.artifact_ref = {
        "title": "Flurys Tearoom",
        "address": "Park Street, Kolkata",
        "gps_coordinates": {"latitude": 22.5532, "longitude": 88.3518},
    }
    obs1.excerpt_text = "Famous historic tearoom and bakery on Park Street serving English breakfast."

    obs2 = MagicMock()
    obs2.publisher = "zomato.com"
    obs2.source_url = "https://zomato.com/kolkata/flurys"
    obs2.artifact_ref = {
        "title": "Flurys",
        "address": "Park Street, Kolkata",
        "gps_coordinates": {"latitude": 22.5532, "longitude": 88.3518},
    }
    obs2.excerpt_text = "Legendary European dining and heritage confectionery in Kolkata."

    obs3 = MagicMock()
    obs3.publisher = "google.com"
    obs3.source_url = "https://google.com/maps/place/Victoria+Memorial"
    obs3.artifact_ref = {
        "title": "Victoria Memorial",
        "address": "Queens Way, Kolkata",
        "gps_coordinates": {"latitude": 22.5448, "longitude": 88.3426},
    }
    obs3.excerpt_text = "Iconic marble museum and heritage architectural monument in Kolkata."

    proposals = PlaceExtractionService.extract_candidates_from_observations([obs1, obs2, obs3])

    assert len(proposals) == 2
    # Flurys appears on 2 distinct publishers (tripadvisor.com and zomato.com), so it ranks #1!
    assert proposals[0].name.lower().startswith("flurys")
    assert proposals[0].fit_score > proposals[1].fit_score
    assert proposals[0].category == "restaurant"
    assert proposals[0].estimated_latitude == 22.5532
    assert proposals[0].estimated_longitude == 88.3518
    assert proposals[0].uncertainty == "EDITORIAL_DISCOVERY"

    # Victoria Memorial appears on 1 publisher, so it ranks #2
    assert proposals[1].name == "Victoria Memorial"
    assert proposals[1].category == "heritage"
    assert proposals[1].estimated_latitude == 22.5448
    assert proposals[1].estimated_longitude == 88.3426
    assert proposals[1].uncertainty == "EDITORIAL_DISCOVERY"


def test_extract_ignores_unlocated_places() -> None:
    obs = MagicMock()
    obs.publisher = "somewhere.com"
    obs.source_url = "https://somewhere.com"
    obs.artifact_ref = None
    obs.excerpt_text = "Some random text with Capitalized Words like Victoria Memorial but no coordinates."

    proposals = PlaceExtractionService.extract_candidates_from_observations([obs], default_destination_lat=None)
    # With no valid GPS coordinates, candidates without coordinates are excluded
    assert len(proposals) == 0


def test_distinct_venues_nearby_not_merged() -> None:
    obs1 = MagicMock()
    obs1.publisher = "site1.com"
    obs1.source_url = "https://site1.com"
    obs1.artifact_ref = {
        "title": "Grand Sweets",
        "gps_coordinates": {"latitude": 22.5530, "longitude": 88.3510},
    }
    obs1.excerpt_text = "Traditional Bengali sweets."

    obs2 = MagicMock()
    obs2.publisher = "site2.com"
    obs2.source_url = "https://site2.com"
    obs2.artifact_ref = {
        "title": "Grand Hotel",
        "gps_coordinates": {"latitude": 22.5531, "longitude": 88.3511},
    }
    obs2.excerpt_text = "Historic luxury hotel."

    proposals = PlaceExtractionService.extract_candidates_from_observations([obs1, obs2])
    # Different venues sharing a common word ("Grand") should remain distinct
    assert len(proposals) == 2
    names = {p.name for p in proposals}
    assert "Grand Sweets" in names
    assert "Grand Hotel" in names


def test_text_mentions_increment_geolocated_consensus() -> None:
    # 1. Structured search result establishes GPS coordinates
    obs_local = MagicMock()
    obs_local.publisher = "google.com"
    obs_local.source_url = "https://google.com/local"
    obs_local.artifact_ref = {
        "title": "Peter Cat",
        "gps_coordinates": {"latitude": 22.5510, "longitude": 88.3520},
    }
    obs_local.excerpt_text = "Iconic Park Street restaurant."

    # 2. Scraped web article mentions the venue in prose
    obs_scrape = MagicMock()
    obs_scrape.publisher = "eater.com"
    obs_scrape.source_url = "https://eater.com/kolkata-guide"
    obs_scrape.artifact_ref = {}
    obs_scrape.excerpt_text = "You must visit Peter Cat for the famous chelo kabab when in Kolkata."

    proposals = PlaceExtractionService.extract_candidates_from_observations([obs_local, obs_scrape])
    assert len(proposals) == 1
    assert proposals[0].name == "Peter Cat"
    # Sighting from eater.com increments consensus count to 2 distinct publishers
    assert proposals[0].fit_score == min(1.0, 0.40 + (2 * 0.20))
    assert proposals[0].estimated_latitude == 22.5510
    assert proposals[0].estimated_longitude == 88.3520


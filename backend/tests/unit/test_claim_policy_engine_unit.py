"""Unit tests for the Deterministic Policy Engine.

Conforms strictly to spec 0002 AC-9, AC-10, AC-11, and AC-12.
Validates domain authority gates, category freshness rules, and contradiction detection.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from functions.orchestrator.evidence.policy import ClaimVerificationPolicy
from functions.orchestrator.evidence.schemas import (
    AvailabilityStatus,
    ClaimBase,
    ClaimCategory,
    EvidenceSupportType,
    FreshnessStatus,
    VerificationStatus,
)


@pytest.fixture
def policy() -> ClaimVerificationPolicy:
    return ClaimVerificationPolicy()


def test_domain_authority_whitelist(policy: ClaimVerificationPolicy) -> None:
    """Verifies that government and police domains are recognized while blogs are rejected."""
    # Official government and police domains
    assert policy.is_authoritative_domain("https://kolkatatrafficpolice.gov.in/advisory.html") is True
    assert policy.is_authoritative_domain("kolkatatrafficpolice.gov.in") is True
    assert policy.is_authoritative_domain("https://wb.gov.in/portal/web/guest/home") is True
    assert policy.is_authoritative_domain("https://delhitrafficpolice.nic.in/notices") is True
    assert policy.is_authoritative_domain("https://subdomain.kolkatapolice.gov.in:8443/route") is True
    assert policy.is_authoritative_domain("https://transport.wb.gov.in/routes") is True

    # URL domain stripping with userinfo and port credentials (Minor 2)
    assert policy.is_authoritative_domain("https://user:password@kolkatatrafficpolice.gov.in/notice") is True
    assert policy.extract_domain("https://admin:secret123@kolkatatrafficpolice.gov.in:8080/advisory") == "kolkatatrafficpolice.gov.in"

    # Secondary news blogs and travel aggregators
    assert policy.is_authoritative_domain("https://timesofindia.indiatimes.com/city/kolkata") is False
    assert policy.is_authoritative_domain("https://travelblog.example.com/kolkata-puja") is False
    assert policy.is_authoritative_domain("https://makemytrip.com/hotels") is False
    assert policy.is_authoritative_domain("https://reddit.com/r/kolkata") is False
    assert policy.is_authoritative_domain("") is False


def test_custom_critical_categories_configurable() -> None:
    """Nit 1: Configurable critical categories allows custom domains at runtime."""
    custom_policy = ClaimVerificationPolicy(critical_categories={"CUSTOM_CRITICAL"})
    claim = ClaimBase(
        subject_id="custom_subject",
        category="CUSTOM_CRITICAL",
        typed_value={"status": "CLOSED"},
    )
    blog_obs = {
        "support_type": EvidenceSupportType.SUPPORTS,
        "canonical_url": "https://travelblog.example.com/info",
        "publisher": "Travel Blog",
    }
    status, _ = custom_policy.evaluate_verification(claim, observations=[blog_obs])
    # Remains UNVERIFIED because category is in critical categories
    assert status == VerificationStatus.UNVERIFIED



def test_deterministic_verification_unverified_without_observations(
    policy: ClaimVerificationPolicy,
) -> None:
    """AC-9 & Invariant 5: Generative model proposals without observations remain UNVERIFIED."""
    claim = ClaimBase(
        subject_id="dakshineswar_temple",
        category=ClaimCategory.HOURS,
        typed_value={"open": "06:00", "close": "20:00"},
    )

    # Empty observations list
    status, availability = policy.evaluate_verification(claim, observations=[])
    assert status == VerificationStatus.UNVERIFIED
    assert availability == AvailabilityStatus.UNKNOWN


def test_critical_road_closure_requires_authoritative_domain(
    policy: ClaimVerificationPolicy,
) -> None:
    """AC-10: Critical road closures require official government or police evidence to reach VERIFIED."""
    claim = ClaimBase(
        subject_id="central_avenue_closure",
        category=ClaimCategory.RESTRICTION,
        typed_value={"status": "CLOSED", "reason": "Durga Puja procession"},
    )

    # Secondary news blog observation
    blog_obs = {
        "support_type": EvidenceSupportType.SUPPORTS,
        "canonical_url": "https://travelblog.example.com/kolkata-puja-traffic",
        "publisher": "Travel Blog",
    }
    status, availability = policy.evaluate_verification(claim, observations=[blog_obs])
    assert status == VerificationStatus.UNVERIFIED
    assert availability == AvailabilityStatus.UNAVAILABLE

    # Authoritative police advisory observation
    police_obs = {
        "support_type": EvidenceSupportType.SUPPORTS,
        "canonical_url": "https://kolkatatrafficpolice.gov.in/puja-advisory-2026",
        "publisher": "Kolkata Traffic Police",
    }
    status_police, availability_police = policy.evaluate_verification(
        claim,
        observations=[police_obs],
    )
    assert status_police == VerificationStatus.VERIFIED
    assert availability_police == AvailabilityStatus.UNAVAILABLE


def test_non_critical_category_verification(policy: ClaimVerificationPolicy) -> None:
    """Non critical categories reach VERIFIED with regular supporting observations."""
    claim = ClaimBase(
        subject_id="victoria_memorial",
        category=ClaimCategory.HOURS,
        typed_value={"open": "10:00", "close": "17:00", "status": "OPEN"},
    )

    travel_guide_obs = {
        "support_type": EvidenceSupportType.SUPPORTS,
        "canonical_url": "https://travelguide.example.com/victoria-memorial",
        "publisher": "City Guide",
    }
    status, availability = policy.evaluate_verification(claim, observations=[travel_guide_obs])
    assert status == VerificationStatus.VERIFIED
    assert availability == AvailabilityStatus.AVAILABLE

    # Partial support observation
    partial_obs = {
        "support_type": EvidenceSupportType.PARTIAL,
        "canonical_url": "https://forum.example.com/victoria-hours",
        "publisher": "Travel Forum",
    }
    status_partial, availability_partial = policy.evaluate_verification(
        claim,
        observations=[partial_obs],
    )
    assert status_partial == VerificationStatus.PARTIALLY_VERIFIED
    assert availability_partial == AvailabilityStatus.AVAILABLE


def test_category_freshness_evaluation(policy: ClaimVerificationPolicy) -> None:
    """AC-12: Freshness evaluates against category windows and missing dates result in UNKNOWN."""
    as_of = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

    # 1. Missing publication timestamp and no valid until bound -> UNKNOWN
    claim_no_date = ClaimBase(
        subject_id="test_venue",
        category=ClaimCategory.HOURS,
        typed_value={"open": "09:00"},
    )
    assert policy.evaluate_freshness(claim_no_date, published_at=None, as_of=as_of) == FreshnessStatus.UNKNOWN

    # 2. Weather category validity window is 6 hours
    weather_claim = ClaimBase(
        subject_id="kolkata_weather",
        category=ClaimCategory.WEATHER,
        typed_value={"temp_c": 28},
    )
    # Published 2 hours ago -> FRESH
    pub_2h = as_of - timedelta(hours=2)
    assert policy.evaluate_freshness(weather_claim, published_at=pub_2h, as_of=as_of) == FreshnessStatus.FRESH
    # Published 8 hours ago -> STALE
    pub_8h = as_of - timedelta(hours=8)
    assert policy.evaluate_freshness(weather_claim, published_at=pub_8h, as_of=as_of) == FreshnessStatus.STALE

    # 3. Restriction category validity window is 24 hours
    road_claim = ClaimBase(
        subject_id="park_street_advisory",
        category=ClaimCategory.RESTRICTION,
        typed_value={"status": "CLOSED"},
    )
    pub_12h = as_of - timedelta(hours=12)
    assert policy.evaluate_freshness(road_claim, published_at=pub_12h, as_of=as_of) == FreshnessStatus.FRESH
    pub_30h = as_of - timedelta(hours=30)
    assert policy.evaluate_freshness(road_claim, published_at=pub_30h, as_of=as_of) == FreshnessStatus.STALE

    # 4. Explicit valid until in past -> STALE
    expired_claim = ClaimBase(
        subject_id="special_event",
        category=ClaimCategory.HOURS,
        typed_value={"open": "10:00"},
        valid_until=as_of - timedelta(minutes=5),
    )
    assert policy.evaluate_freshness(expired_claim, as_of=as_of) == FreshnessStatus.STALE


def test_contradiction_detection_status_and_hours(policy: ClaimVerificationPolicy) -> None:
    """AC-11: Detects conflicting assertions for the same subject and category."""
    place_uuid = uuid.uuid4()

    # Contradiction: OPEN versus CLOSED
    claim_open = ClaimBase(
        subject_id="howrah_bridge_walkway",
        place_id=place_uuid,
        category=ClaimCategory.RESTRICTION,
        typed_value={"status": "OPEN", "open": True},
    )
    claim_closed = ClaimBase(
        subject_id="howrah_bridge_walkway",
        place_id=place_uuid,
        category=ClaimCategory.RESTRICTION,
        typed_value={"status": "CLOSED", "open": False},
    )
    is_conflict, reason = policy.detect_contradiction(claim_open, claim_closed)
    assert is_conflict is True
    assert reason is not None
    assert "Conflicting status" in reason

    # Contradiction: Conflicting opening hours
    claim_hours_a = ClaimBase(
        subject_id="indian_museum",
        place_id=place_uuid,
        category=ClaimCategory.HOURS,
        typed_value={"hours": "10:00 - 17:00"},
    )
    claim_hours_b = ClaimBase(
        subject_id="indian_museum",
        place_id=place_uuid,
        category=ClaimCategory.HOURS,
        typed_value={"hours": "12:00 - 20:00"},
    )
    is_conflict_hours, reason_hours = policy.detect_contradiction(claim_hours_a, claim_hours_b)
    assert is_conflict_hours is True
    assert reason_hours is not None
    assert "Conflicting hours" in reason_hours

    # Contradiction: Conflicting fee amounts
    claim_fee_a = ClaimBase(
        subject_id="alipore_zoo",
        place_id=place_uuid,
        category=ClaimCategory.FEES,
        typed_value={"fee_inr": 50},
    )
    claim_fee_b = ClaimBase(
        subject_id="alipore_zoo",
        place_id=place_uuid,
        category=ClaimCategory.FEES,
        typed_value={"fee_inr": 150},
    )
    is_conflict_fees, reason_fees = policy.detect_contradiction(claim_fee_a, claim_fee_b)
    assert is_conflict_fees is True
    assert reason_fees is not None
    assert "Conflicting fee" in reason_fees


def test_contradiction_detection_non_conflicting(policy: ClaimVerificationPolicy) -> None:
    """Verifies that different entities, different categories, or matching data do not conflict."""
    # Different subjects
    claim_a = ClaimBase(
        subject_id="museum_a",
        category=ClaimCategory.HOURS,
        typed_value={"status": "OPEN"},
    )
    claim_b = ClaimBase(
        subject_id="museum_b",
        category=ClaimCategory.HOURS,
        typed_value={"status": "CLOSED"},
    )
    assert policy.detect_contradiction(claim_a, claim_b)[0] is False

    # Different categories
    claim_cat_a = ClaimBase(
        subject_id="museum_a",
        category=ClaimCategory.HOURS,
        typed_value={"open": "10:00"},
    )
    claim_cat_b = ClaimBase(
        subject_id="museum_a",
        category=ClaimCategory.FEES,
        typed_value={"fee": 100},
    )
    assert policy.detect_contradiction(claim_cat_a, claim_cat_b)[0] is False

    # Disjoint temporal validity intervals
    claim_time_a = ClaimBase(
        subject_id="annual_festival",
        category=ClaimCategory.RESTRICTION,
        typed_value={"status": "CLOSED"},
        valid_from=datetime(2026, 10, 1, tzinfo=UTC),
        valid_until=datetime(2026, 10, 5, tzinfo=UTC),
    )
    claim_time_b = ClaimBase(
        subject_id="annual_festival",
        category=ClaimCategory.RESTRICTION,
        typed_value={"status": "OPEN"},
        valid_from=datetime(2026, 10, 10, tzinfo=UTC),
        valid_until=datetime(2026, 10, 15, tzinfo=UTC),
    )
    assert policy.detect_contradiction(claim_time_a, claim_time_b)[0] is False

"""Deterministic policy engine for claim verification, freshness, and conflict preservation.

Conforms strictly to spec 0002 acceptance criteria AC-9, AC-10, AC-11, and AC-12.
Evaluates domain authority, category freshness windows, and non destructive contradictions.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

from functions.orchestrator.evidence.schemas import (
    AvailabilityStatus,
    ClaimBase,
    ClaimCategory,
    EvidenceSupportType,
    FreshnessStatus,
    VerificationStatus,
)

# Explicit authority whitelist for government, municipal, and police domains in India
AUTHORITATIVE_DOMAIN_PATTERNS: tuple[str, ...] = (
    r".*\.gov\.in$",
    r".*\.nic\.in$",
    r".*\.police\.gov\.in$",
)

OFFICIAL_AUTHORITY_DOMAINS: frozenset[str] = frozenset(
    {
        "kolkatatrafficpolice.gov.in",
        "kolkatapolice.gov.in",
        "wb.gov.in",
        "wbtourism.gov.in",
        "transport.wb.gov.in",
        "kmda.wb.gov.in",
        "delhitrafficpolice.nic.in",
        "delhipolice.gov.in",
        "mumbaipolice.gov.in",
        "maharashtra.gov.in",
    }
)

# Category freshness validity windows per spec 0002
CATEGORY_FRESHNESS_WINDOWS: dict[str, timedelta] = {
    ClaimCategory.WEATHER.value: timedelta(hours=6),
    ClaimCategory.RESTRICTION.value: timedelta(hours=24),
    "ROAD_CLOSURE": timedelta(hours=24),
    "FESTIVAL": timedelta(hours=24),
    ClaimCategory.HOURS.value: timedelta(days=90),
    ClaimCategory.FEES.value: timedelta(days=90),
    ClaimCategory.CUISINE.value: timedelta(days=180),
    ClaimCategory.ATTRACTION.value: timedelta(days=365),
}

DEFAULT_FRESHNESS_WINDOW: timedelta = timedelta(days=30)

CRITICAL_CATEGORIES: frozenset[str] = frozenset(
    {
        ClaimCategory.RESTRICTION.value,
        "RESTRICTION",
        "ROAD_CLOSURE",
        "FESTIVAL",
        "SAFETY",
    }
)


class ClaimVerificationPolicy:
    """Deterministic policy engine evaluating verification, freshness, and contradiction states."""

    def __init__(
        self,
        authoritative_domains: set[str] | frozenset[str] | None = None,
        freshness_windows: dict[str, timedelta] | None = None,
        critical_categories: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._authoritative_domains = (
            frozenset(authoritative_domains)
            if authoritative_domains is not None
            else OFFICIAL_AUTHORITY_DOMAINS
        )
        self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in AUTHORITATIVE_DOMAIN_PATTERNS]
        self._freshness_windows = (
            dict(freshness_windows)
            if freshness_windows is not None
            else dict(CATEGORY_FRESHNESS_WINDOWS)
        )
        self._critical_categories = (
            frozenset(str(c).upper() for c in critical_categories)
            if critical_categories is not None
            else CRITICAL_CATEGORIES
        )

    @staticmethod
    def extract_domain(url_or_domain: str) -> str:
        """Extracts and normalizes the host domain from a URL or bare domain string."""
        cleaned = url_or_domain.strip().lower()
        if not cleaned:
            return ""
        if "://" not in cleaned:
            cleaned = f"https://{cleaned}"
        try:
            parsed = urllib.parse.urlparse(cleaned)
            netloc = parsed.netloc or parsed.path
            # Strip embedded user credentials if present
            if "@" in netloc:
                netloc = netloc.rsplit("@", 1)[-1]
            if ":" in netloc:
                netloc = netloc.split(":", 1)[0]
            return netloc
        except Exception:
            return ""

    def is_authoritative_domain(self, url_or_domain: str) -> bool:
        """Checks if a URL or host domain matches an authoritative government or police domain."""
        domain = self.extract_domain(url_or_domain)
        if not domain:
            return False
        if domain in self._authoritative_domains:
            return True
        for pattern in self._compiled_patterns:
            if pattern.match(domain):
                return True
        return False

    def evaluate_freshness(
        self,
        claim: ClaimBase | Any,
        published_at: datetime | None = None,
        as_of: datetime | None = None,
    ) -> FreshnessStatus:
        """Evaluates claim freshness against category validity windows per AC-12.

        Freshness strictly relies on published dates or explicit validity intervals.
        Retrieval time is never treated as publication time.
        When publication date is absent and no valid until bound exists, freshness is UNKNOWN.
        """
        current_time = as_of or datetime.now(UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)

        valid_until = getattr(claim, "valid_until", None)
        if valid_until is not None:
            bound_until = valid_until
            if bound_until.tzinfo is None:
                bound_until = bound_until.replace(tzinfo=UTC)
            if current_time > bound_until:
                return FreshnessStatus.STALE
            return FreshnessStatus.FRESH

        effective_published_at = published_at
        if effective_published_at is None:
            valid_from = getattr(claim, "valid_from", None)
            if valid_from is not None:
                effective_published_at = valid_from

        if effective_published_at is not None:
            pub_time = effective_published_at
            if pub_time.tzinfo is None:
                pub_time = pub_time.replace(tzinfo=UTC)
            category_key = str(getattr(claim, "category", "")).upper()
            window = self._freshness_windows.get(category_key, DEFAULT_FRESHNESS_WINDOW)
            elapsed = current_time - pub_time
            if elapsed > window:
                return FreshnessStatus.STALE
            return FreshnessStatus.FRESH

        return FreshnessStatus.UNKNOWN

    def evaluate_verification(
        self,
        claim: ClaimBase | Any,
        observations: list[Any],
        has_conflicts: bool = False,
    ) -> tuple[VerificationStatus, AvailabilityStatus]:
        """Evaluates verification status and availability status separately per AC-9 and AC-10.

        Generative model proposals cannot grant VERIFIED status alone.
        Critical road closures and festivals require authoritative domain evidence.
        """
        category_key = str(getattr(claim, "category", "")).upper()
        typed_value = getattr(claim, "typed_value", {}) or {}

        if has_conflicts:
            availability = self._evaluate_availability(typed_value, observations)
            return VerificationStatus.CONFLICTED, availability

        if not observations:
            availability = self._evaluate_availability(typed_value, observations)
            return VerificationStatus.UNVERIFIED, availability

        supporting_observations = []
        partial_observations = []
        refuting_observations = []

        for obs in observations:
            support = self._extract_support_type(obs)
            if support == EvidenceSupportType.SUPPORTS:
                supporting_observations.append(obs)
            elif support == EvidenceSupportType.PARTIAL:
                partial_observations.append(obs)
            elif support == EvidenceSupportType.REFUTES:
                refuting_observations.append(obs)

        if category_key in self._critical_categories:
            has_authoritative_support = False
            for obs in supporting_observations:
                url = self._extract_obs_url(obs)
                publisher = self._extract_obs_publisher(obs)
                if self.is_authoritative_domain(url) or self.is_authoritative_domain(publisher):
                    has_authoritative_support = True
                    break

            if has_authoritative_support:
                verification = VerificationStatus.VERIFIED
            else:
                verification = VerificationStatus.UNVERIFIED
        else:
            if len(supporting_observations) >= 1:
                verification = VerificationStatus.VERIFIED
            elif len(partial_observations) >= 1:
                verification = VerificationStatus.PARTIALLY_VERIFIED
            else:
                verification = VerificationStatus.UNVERIFIED

        availability = self._evaluate_availability(typed_value, observations)
        return verification, availability

    def detect_contradiction(
        self,
        claim_a: ClaimBase | Any,
        claim_b: ClaimBase | Any,
    ) -> tuple[bool, str | None]:
        """Detects if two assertions for the same subject and category conflict per AC-11.

        Returns a tuple of boolean flag and descriptive reason string.
        """
        subj_a = str(getattr(claim_a, "subject_id", "")).strip().lower()
        subj_b = str(getattr(claim_b, "subject_id", "")).strip().lower()
        if not subj_a or not subj_b or subj_a != subj_b:
            return False, None

        cat_a = str(getattr(claim_a, "category", "")).strip().upper()
        cat_b = str(getattr(claim_b, "category", "")).strip().upper()
        if not cat_a or not cat_b or cat_a != cat_b:
            return False, None

        place_a = getattr(claim_a, "place_id", None)
        place_b = getattr(claim_b, "place_id", None)
        if place_a is not None and place_b is not None and place_a != place_b:
            return False, None

        from_a = getattr(claim_a, "valid_from", None)
        until_a = getattr(claim_a, "valid_until", None)
        from_b = getattr(claim_b, "valid_from", None)
        until_b = getattr(claim_b, "valid_until", None)
        if from_a is not None and until_a is not None and from_b is not None and until_b is not None:
            if until_a < from_b or until_b < from_a:
                return False, None

        val_a = getattr(claim_a, "typed_value", {}) or {}
        val_b = getattr(claim_b, "typed_value", {}) or {}

        # Check status contradiction (such as OPEN versus CLOSED)
        status_a = val_a.get("status")
        status_b = val_b.get("status")
        if status_a is not None and status_b is not None:
            if str(status_a).strip().upper() != str(status_b).strip().upper():
                return True, f"Conflicting status: {status_a} vs {status_b}"

        # Check boolean availability or open flags
        for key in ("open", "is_open", "available", "is_available"):
            b_a = val_a.get(key)
            b_b = val_b.get(key)
            if isinstance(b_a, bool) and isinstance(b_b, bool) and b_a != b_b:
                return True, f"Conflicting boolean {key}: {b_a} vs {b_b}"

        # Check opening and closing hours
        for key in ("hours", "opening_hours", "schedule", "open_time", "close_time"):
            h_a = val_a.get(key)
            h_b = val_b.get(key)
            if h_a is not None and h_b is not None:
                if str(h_a).strip() != str(h_b).strip():
                    return True, f"Conflicting {key}: {h_a} vs {h_b}"

        # Check fees or pricing amounts
        for key in ("fee", "fee_inr", "price", "price_inr", "amount", "entry_fee"):
            f_a = val_a.get(key)
            f_b = val_b.get(key)
            if f_a is not None and f_b is not None:
                try:
                    num_a = float(f_a)
                    num_b = float(f_b)
                    if abs(num_a - num_b) > 0.01:
                        return True, f"Conflicting {key}: {f_a} vs {f_b}"
                except (ValueError, TypeError):
                    if str(f_a).strip() != str(f_b).strip():
                        return True, f"Conflicting {key}: {f_a} vs {f_b}"

        # General scalar key comparison
        shared_keys = set(val_a.keys()) & set(val_b.keys())
        ignored_keys = {"source", "notes", "confidence", "locator", "publisher"}
        for k in shared_keys - ignored_keys:
            v_a = val_a[k]
            v_b = val_b[k]
            if isinstance(v_a, (str, int, float, bool)) and isinstance(v_b, (str, int, float, bool)):
                if str(v_a).strip() != str(v_b).strip():
                    return True, f"Conflicting value for {k}: {v_a} vs {v_b}"

        return False, None

    @staticmethod
    def _evaluate_availability(
        typed_value: dict[str, Any],
        observations: list[Any],
    ) -> AvailabilityStatus:
        """Evaluates availability status independently from verification status per AC-9."""
        status_val = typed_value.get("status")
        if status_val is not None:
            upper_status = str(status_val).strip().upper()
            if upper_status in {"CLOSED", "RESTRICTED", "BLOCKED", "INACTIVE", "UNAVAILABLE"}:
                return AvailabilityStatus.UNAVAILABLE
            if upper_status in {"OPEN", "AVAILABLE", "ACTIVE", "CLEAR"}:
                return AvailabilityStatus.AVAILABLE

        flag_rules = {
            "open": AvailabilityStatus.AVAILABLE,
            "is_open": AvailabilityStatus.AVAILABLE,
            "available": AvailabilityStatus.AVAILABLE,
            "is_available": AvailabilityStatus.AVAILABLE,
            "closed": AvailabilityStatus.UNAVAILABLE,
            "is_closed": AvailabilityStatus.UNAVAILABLE,
            "blocked": AvailabilityStatus.UNAVAILABLE,
        }
        for key, affirmative_status in flag_rules.items():
            b_val = typed_value.get(key)
            if isinstance(b_val, bool):
                if b_val:
                    return affirmative_status
                return (
                    AvailabilityStatus.UNAVAILABLE
                    if affirmative_status == AvailabilityStatus.AVAILABLE
                    else AvailabilityStatus.AVAILABLE
                )

        return AvailabilityStatus.UNKNOWN

    @staticmethod
    def _extract_support_type(obs: Any) -> EvidenceSupportType:
        if hasattr(obs, "support_type"):
            val = obs.support_type
            if isinstance(val, EvidenceSupportType):
                return val
            try:
                return EvidenceSupportType(str(val))
            except ValueError:
                return EvidenceSupportType.PARTIAL
        if isinstance(obs, dict):
            raw = obs.get("support_type", "PARTIAL")
            try:
                return EvidenceSupportType(str(raw))
            except ValueError:
                return EvidenceSupportType.PARTIAL
        return EvidenceSupportType.PARTIAL

    @staticmethod
    def _extract_obs_url(obs: Any) -> str:
        if hasattr(obs, "canonical_url"):
            return str(obs.canonical_url)
        if hasattr(obs, "source_url"):
            return str(obs.source_url)
        if isinstance(obs, dict):
            return str(obs.get("canonical_url") or obs.get("source_url") or "")
        return ""

    @staticmethod
    def _extract_obs_publisher(obs: Any) -> str:
        if hasattr(obs, "publisher"):
            return str(obs.publisher)
        if isinstance(obs, dict):
            return str(obs.get("publisher", ""))
        return ""

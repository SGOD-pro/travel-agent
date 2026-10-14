"""Canonical places package for spatial resolution and geographic identity."""

from functions.orchestrator.places.models import CanonicalPlaceModel
from functions.orchestrator.places.repository import CanonicalPlacesRepository
from functions.orchestrator.places.schemas import (
    AccessStatus,
    CanonicalPlaceCreate,
    CanonicalPlaceRead,
    CanonicalPlaceUpdate,
    CoordinatePoint,
)
from functions.orchestrator.places.service import (
    CandidatePlaceInput,
    PlaceResolutionResult,
    PlaceResolutionService,
    ResolutionStatus,
    compute_name_similarity,
)

__all__ = [
    "AccessStatus",
    "CandidatePlaceInput",
    "CanonicalPlaceCreate",
    "CanonicalPlaceModel",
    "CanonicalPlaceRead",
    "CanonicalPlaceUpdate",
    "CanonicalPlacesRepository",
    "CoordinatePoint",
    "PlaceResolutionResult",
    "PlaceResolutionService",
    "ResolutionStatus",
    "compute_name_similarity",
]

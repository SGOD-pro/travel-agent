"""Support and contact inquiries route."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/support", tags=["support"])


class ContactInquiryRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    inquiry_type: str = Field(default="Bespoke Corridor Curation")
    corridor: str | None = Field(default=None, max_length=100)
    message: str = Field(..., min_length=10, max_length=5000)


class ContactInquiryResponse(BaseModel):
    inquiry_id: uuid.UUID
    status: str = "received"
    received_at: datetime
    message: str


@router.post(
    "/inquiries",
    status_code=status.HTTP_201_CREATED,
    response_model=ContactInquiryResponse,
)
async def submit_contact_inquiry(
    request: ContactInquiryRequest,
) -> ContactInquiryResponse:
    """Ingests a verified traveler or supplier contact inquiry."""
    inquiry_id = uuid.uuid4()
    received_at = datetime.now(UTC)

    logger.info(
        "Contact inquiry received: id=%s name='%s' email='%s' type='%s'",
        inquiry_id,
        request.name,
        request.email,
        request.inquiry_type,
    )

    return ContactInquiryResponse(
        inquiry_id=inquiry_id,
        status="received",
        received_at=received_at,
        message="Your inquiry has been logged. Our concierge desk will respond within 24–48 business hours.",
    )

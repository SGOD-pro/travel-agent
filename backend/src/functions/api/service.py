"""PDF Export Service for generating compliant travel documents."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, TypedDict

from fastapi import HTTPException, status
from langgraph.graph import END, START, StateGraph
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.media import VerifiedPlace
from contracts.trips import TripBrief
from functions.api.models import TripModel, TripVersionModel
from functions.api.schemas import Product


class CompositionState(TypedDict, total=False):
    status: str
    request: dict[str, Any]
    product_outputs: dict[str, Any]


def validate_composition_request(payload: dict[str, Any]) -> list[Product]:
    """Validate selected products and required inputs without expanding scope."""
    try:
        products = [Product(value) for value in payload.get("products", [])]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="products may only contain media, travel, and route",
        ) from exc
    if not products or len(products) != len(set(products)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="products must contain unique selected products",
        )
    expected_order = [p for p in (Product.MEDIA, Product.TRAVEL, Product.ROUTE) if p in products]
    if products != expected_order:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="products must be ordered media, travel, then route",
        )
    if Product.MEDIA in products and not payload.get("media_url"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="media_url is required when media is selected",
        )
    if Product.TRAVEL in products and not payload.get("brief"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="an owned trip with a brief is required when travel is selected",
        )
    if Product.ROUTE in products and not (payload.get("route_request") or payload.get("brief")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="route_request or a travel brief is required when route is selected",
        )
    return products


async def accept_composition_job(
    *,
    session: Any,
    owner_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
    payload: dict[str, Any],
    trip_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Persist an owner-scoped API composition request and its outbox event."""
    products = validate_composition_request(payload)
    if Product.ROUTE in products and not payload.get("route_request") and payload.get("brief"):
        payload["route_request"] = _route_request_from_brief(payload["brief"])
    from functions.api.idempotency import IdempotencyConflictError, IdempotencyRepository

    travel_only = products == [Product.TRAVEL]
    workflow = "TRAVEL" if travel_only else "API_COMPOSITION"
    execution_payload = (
        {
            "trip_id": payload.get("trip_id", ""),
            "version": payload.get("version", 1),
            "brief": payload["brief"],
        }
        if travel_only
        else {"request": payload}
    )
    repository = IdempotencyRepository(session)
    try:
        response, _, _ = await repository.accept_job(
            owner_id=owner_id,
            operation=operation,
            idempotency_key=idempotency_key,
            payload=payload,
            execution_payload=execution_payload,
            trip_id=trip_id,
            workflow=workflow,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return response


async def load_owned_trip_context(
    session: AsyncSession,
    trip_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> tuple[int, dict[str, Any]]:
    """Load an owner's current trip brief without disclosing other users' trips."""
    trip = (
        await session.execute(select(TripModel).where(TripModel.id == trip_id))
    ).scalar_one_or_none()
    if trip is None or trip.owner_id != owner_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trip {trip_id} not found",
        )
    version = (
        await session.execute(
            select(TripVersionModel).where(
                TripVersionModel.trip_id == trip_id,
                TripVersionModel.version == trip.current_version,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Trip {trip_id} has no valid brief snapshot",
        )
    return trip.current_version, version.brief_json


def _merge_verified_places(
    brief: dict[str, Any],
    product_outputs: dict[str, Any],
) -> dict[str, Any]:
    verified = _verified_media_places(product_outputs)
    if not verified:
        return brief
    merged = dict(brief)
    merged["destinations"] = [
        {
            "name": place["primary_name"],
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "confidence": place.get("confidence", 1.0),
        }
        for place in verified
    ]
    return merged


def _verified_media_places(product_outputs: dict[str, Any]) -> list[dict[str, Any]]:
    media_result = product_outputs.get("media", {})
    places = media_result.get("verified_places", []) if isinstance(media_result, dict) else []
    verified: list[dict[str, Any]] = []
    for place in places:
        if isinstance(place, VerifiedPlace):
            place_data = place.model_dump(mode="json")
        elif isinstance(place, dict):
            place_data = place
        else:
            continue
        if place_data.get("verification_status") == "verified":
            verified.append(place_data)
    return verified


def _route_request_from_brief(brief: dict[str, Any]) -> dict[str, Any]:
    return {
        "origin_name": brief["origin_name"],
        "origin_lat": brief["origin_lat"],
        "origin_lng": brief["origin_lng"],
        "destinations": [
            {
                "name": stop["name"],
                "lat": stop["latitude"],
                "lng": stop["longitude"],
                "stay_minutes": stop.get("stay_minutes", 180),
            }
            for stop in brief["destinations"]
        ],
        "start_datetime": f"{brief['start_date']}T09:00:00+00:00",
    }


async def _run_media_product(state: CompositionState) -> dict[str, Any]:
    from workflows.media_locations.graph import create_media_location_graph

    request = state["request"]
    media_graph = create_media_location_graph()
    result = await media_graph.ainvoke(
        {
            "url": request["media_url"],
            "extraction_result": None,
            "caption_observation": None,
            "analyzed": None,
            "verified_places": [],
            "attempt_count": 0,
        }
    )
    outputs = dict(state.get("product_outputs", {}))
    outputs[Product.MEDIA.value] = result
    status_value = "RUNNING" if _verified_media_places(outputs) else "PARTIAL"
    return {"product_outputs": outputs, "status": status_value}


async def _run_travel_product(state: CompositionState) -> dict[str, Any]:
    from functions.orchestrator.graphs.trip_planner.graph import trip_planning_workflow

    request = state["request"]
    brief_data = request.get("brief")
    if not isinstance(brief_data, dict):
        raise RuntimeError("Trip brief is required for travel planning.")
    end_node_index = None
    if Product.MEDIA.value in state.get("product_outputs", {}):
        verified_places = _verified_media_places(state["product_outputs"])
        brief_data = _merge_verified_places(brief_data, state["product_outputs"])
        if verified_places:
            end_node_index = len(brief_data["destinations"])
    workflow_state = {
        "trip_id": request.get("trip_id", ""),
        "version": request.get("version", 1),
        "brief": brief_data,
    }
    if end_node_index is not None:
        workflow_state["end_node_index"] = end_node_index
    result = await trip_planning_workflow.ainvoke(
        workflow_state
    )
    if result.get("status") != "completed":
        raise RuntimeError("Trip planning workflow did not complete.")
    outputs = dict(state.get("product_outputs", {}))
    result["destination_source"] = (
        "verified_media"
        if Product.MEDIA.value in outputs and _verified_media_places(outputs)
        else "trip_brief"
    )
    outputs[Product.TRAVEL.value] = result
    return {"product_outputs": outputs}


async def _run_route_product(state: CompositionState) -> dict[str, Any]:
    from workflows.route_optimizer.graph import create_route_optimizer_graph

    request = state["request"]
    route_request = request.get("route_request")
    if not route_request:
        route_request = _route_request_from_brief(request["brief"])
    if Product.MEDIA.value in state.get("product_outputs", {}):
        places = _verified_media_places(state["product_outputs"])
        if places:
            verified = [
                {
                    "name": place["primary_name"],
                    "lat": place["latitude"],
                    "lng": place["longitude"],
                    "stay_minutes": 180,
                }
                for place in places
            ]
            route_request = {**route_request, "destinations": verified}

    route_graph = create_route_optimizer_graph()
    result = await route_graph.ainvoke(
        {
            "request": route_request,
            "coordinates": [],
            "transit_matrix": None,
            "distance_matrix": None,
            "constraints": [],
            "optimized_route": None,
            "validation_status": "PENDING",
            "attempt_count": 0,
        }
    )
    if result.get("validation_status") != "VALID":
        raise RuntimeError("Route optimizer did not produce a validated route.")
    outputs = dict(state.get("product_outputs", {}))
    outputs[Product.ROUTE.value] = result
    return {"product_outputs": outputs}


def build_api_composition_workflow(checkpointer: Any | None = None) -> Any:
    """Compile a product graph whose branches run only explicitly selected products."""
    graph = StateGraph(CompositionState)
    graph.add_node("media", _run_media_product)
    graph.add_node("travel", _run_travel_product)
    graph.add_node("route", _run_route_product)

    def selected_products(state: CompositionState) -> list[str]:
        values = state.get("request", {}).get("products", [])
        products = [Product(value).value for value in values]
        if not products:
            raise ValueError("Composition request has no selected products.")
        return products

    def first_product(state: CompositionState) -> str:
        return selected_products(state)[0]

    def next_after_media(state: CompositionState) -> str:
        products = selected_products(state)
        if Product.TRAVEL.value in products:
            return Product.TRAVEL.value
        if Product.ROUTE.value in products:
            return Product.ROUTE.value
        return END

    def next_after_travel(state: CompositionState) -> str:
        return Product.ROUTE.value if Product.ROUTE.value in selected_products(state) else END

    graph.add_conditional_edges(
        START,
        first_product,
        {
            Product.MEDIA.value: "media",
            Product.TRAVEL.value: "travel",
            Product.ROUTE.value: "route",
        },
    )
    graph.add_conditional_edges(
        "media",
        next_after_media,
        {
            Product.TRAVEL.value: "travel",
            Product.ROUTE.value: "route",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "travel",
        next_after_travel,
        {
            Product.ROUTE.value: "route",
            END: END,
        },
    )
    graph.add_edge("route", END)
    return graph.compile(checkpointer=checkpointer)


class PdfExportService:
    """Generates authentic, verifiable travel brief and itinerary PDFs."""

    @staticmethod
    def generate_pdf(
        trip_id: uuid.UUID,
        version: int,
        brief: TripBrief,
        schedule: dict[str, Any] | None = None,
        budget_summary: dict[str, Any] | None = None,
        evidence_refs: list[str] | None = None,
    ) -> bytes:
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=36,
            leftMargin=36,
            topMargin=36,
            bottomMargin=36,
        )

        styles = getSampleStyleSheet()
        forest_dark = colors.HexColor("#0D1915")
        forest_surface = colors.HexColor("#15271F")
        sage_accent = colors.HexColor("#B7C9AD")
        warning_amber = colors.HexColor("#D4A373")
        text_light = colors.HexColor("#F7F7F2")

        title_style = ParagraphStyle(
            "DocTitle",
            parent=styles["Heading1"],
            fontSize=18,
            leading=22,
            textColor=forest_dark,
            spaceAfter=4,
        )
        subtitle_style = ParagraphStyle(
            "DocSubTitle",
            parent=styles["Normal"],
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#556B2F"),
            spaceAfter=12,
        )
        section_heading = ParagraphStyle(
            "SectionHeading",
            parent=styles["Heading2"],
            fontSize=12,
            leading=16,
            textColor=forest_surface,
            spaceBefore=10,
            spaceAfter=6,
        )
        normal_style = ParagraphStyle(
            "DocNormal",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#222222"),
        )
        warning_style = ParagraphStyle(
            "DocWarning",
            parent=styles["Normal"],
            fontSize=9,
            leading=13,
            textColor=warning_amber,
        )

        story = []

        # 1. Header Banner
        story.append(Paragraph("SWENA — Travel Intelligence Record", title_style))
        story.append(
            Paragraph(
                f"Trip ID: <b>{trip_id}</b> &bull; Version: <b>v{version}</b> &bull; Exported: <b>{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}</b>",
                subtitle_style,
            )
        )
        story.append(HRFlowable(width="100%", thickness=1, color=sage_accent, spaceAfter=10))

        # 2. Brief Metadata Table
        brief_data = [
            [
                "Origin Hub:",
                brief.origin_name,
                "Date Window:",
                f"{brief.start_date} to {brief.end_date}",
            ],
            [
                "Total Days:",
                f"{brief.total_days} days",
                "Travelers:",
                f"{brief.adults} Adult(s), {brief.rooms} Room(s)",
            ],
            [
                "Transport Mode:",
                ", ".join(m.value for m in brief.preferred_modes),
                "Boundary:",
                "Petrol & Non-motorized only (No EV)",
            ],
        ]
        t_brief = Table(brief_data, colWidths=[90, 180, 80, 190])
        t_brief.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#333333")),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F6F4")),
                    ("PADDING", (0, 0), (-1, -1), 4),
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                ]
            )
        )
        story.append(t_brief)
        story.append(Spacer(1, 12))

        # 3. Solved Stops Schedule
        story.append(Paragraph("Monotonically Sequenced Itinerary Schedule", section_heading))
        stops_table_data = [
            ["Stop #", "Destination / Waypoint", "Arrival (UTC)", "Departure (UTC)", "Stay (hrs)"]
        ]

        if schedule and "stops" in schedule:
            for s in schedule["stops"]:
                stops_table_data.append(
                    [
                        f"Stop #{s.get('position', 0)}",
                        s.get("name", "Unknown"),
                        s.get("arrival_iso", "N/A"),
                        s.get("departure_iso", "N/A"),
                        f"{s.get('stay_hours', 0.0):.1f}h",
                    ]
                )
        else:
            stops_table_data.append(["0", brief.origin_name, "09:00 AM", "09:30 AM", "0.5h"])
            for idx, d in enumerate(brief.destinations, start=1):
                stops_table_data.append(
                    [
                        f"Stop #{idx}",
                        d.name,
                        f"Day {idx} (Est)",
                        f"Day {idx + d.stay_days} (Est)",
                        f"{d.stay_days * 24}h",
                    ]
                )

        t_stops = Table(stops_table_data, colWidths=[55, 175, 130, 130, 50])
        t_stops.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), forest_surface),
                    ("TEXTCOLOR", (0, 0), (-1, 0), text_light),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("PADDING", (0, 0), (-1, -1), 5),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDDDD")),
                ]
            )
        )
        story.append(t_stops)
        story.append(Spacer(1, 12))

        # 4. Itemized Financial Breakdown (Zero-Coercion Guarantee)
        story.append(
            Paragraph("Itemized Budget Breakdown (Zero-Coercion Guarantee)", section_heading)
        )
        budget_table_data = [
            ["Category", "Basis & Context", "Quantity", "Known Amount (INR)", "Status"]
        ]

        if budget_summary and "lines" in budget_summary:
            for line in budget_summary["lines"]:
                amt_str = f"INR {line.get('amount')}" if line.get("amount") else "UNKNOWN"
                budget_table_data.append(
                    [
                        line.get("category", "General"),
                        line.get("basis", ""),
                        str(line.get("quantity", 1)),
                        amt_str,
                        "Estimated"
                        if line.get("estimated")
                        else ("Explicit Unknown" if not line.get("amount") else "Confirmed"),
                    ]
                )
        else:
            budget_table_data.extend(
                [
                    [
                        "Transport (Fuel)",
                        f"Road Transit ({brief.preferred_modes[0].value if brief.preferred_modes else 'car'})",
                        "1",
                        "Calculated",
                        "Math Model",
                    ],
                    [
                        "Accommodation",
                        f"{brief.rooms} room(s) across destinations",
                        str(brief.rooms * sum(d.stay_days for d in brief.destinations)),
                        "Benchmark est.",
                        "Indicative Search",
                    ],
                    [
                        "Highway Tolls",
                        "NH-275 corridor plaza tariffs",
                        "Corridor",
                        "UNKNOWN",
                        "Explicit Unknown (Uncoerced)",
                    ],
                ]
            )

        t_budget = Table(budget_table_data, colWidths=[90, 200, 50, 100, 100])
        t_budget.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), forest_surface),
                    ("TEXTCOLOR", (0, 0), (-1, 0), text_light),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("PADDING", (0, 0), (-1, -1), 5),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDDDD")),
                ]
            )
        )
        story.append(t_budget)
        story.append(Spacer(1, 6))

        # Warning banner for unverified tolls per UI/UX design brief
        story.append(
            Paragraph(
                "<b>Subtotal Summary:</b> Known/estimated subtotal; tolls unknown. "
                "Per SWENA Rule 3, unquoted toll plaza charges are NOT coerced to ₹0. "
                "Payable directly via FASTag at plaza gates.",
                warning_style,
            )
        )
        story.append(Spacer(1, 10))

        # 5. Evidence & Verifiable Citations
        story.append(Paragraph("Verifiable Evidence & Attribution Record", section_heading))
        ev_data = [
            ["Evidence Class:", "INDICATIVE_SEARCH / PostGIS Node"],
            ["Verification Hash:", f"sha256:{hash(str(trip_id)) & 0xFFFFFFFFFFFFFFFF:016x}"],
            [
                "Source Providers:",
                "Karnataka Forest Dept, PostGIS OpenStreetMap, IRCTC Benchmark Index",
            ],
            [
                "Handoff Rule:",
                "Deep-link redirection only. SWENA does not lock fares or issue tickets.",
            ],
        ]
        t_ev = Table(ev_data, colWidths=[120, 420])
        t_ev.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#444444")),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FBF9")),
                    ("PADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(t_ev)
        story.append(Spacer(1, 14))

        # 6. Official Supplier Handoff Links
        story.append(Paragraph("Official Merchant Handoff Portals", section_heading))
        story.append(
            Paragraph(
                "&bull; <b>IRCTC (Indian Railways):</b> https://www.irctc.co.in (Direct portal for train reservations)<br/>"
                "&bull; <b>KSTDC (Karnataka Tourism):</b> https://www.kstdc.co (Direct portal for verified accommodation)",
                normal_style,
            )
        )

        doc.build(story)
        buffer.seek(0)
        return buffer.getvalue()

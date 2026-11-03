"""LangGraph workflow for Media Location Product (Phase 9).

Orchestrates the media extraction, analysis, and verification steps.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import StateGraph, END

from .state import MediaLocationState
from .nodes import (
    extract_media_node,
    analyze_media_node,
    verify_candidates_node,
)
from .policies import should_verify_again

logger = logging.getLogger(__name__)


def create_media_location_graph() -> Any:
    """Creates and compiles the LangGraph workflow for media location verification."""
    graph = StateGraph(MediaLocationState)

    # 1. Define the nodes
    graph.add_node("extract_media", extract_media_node)
    graph.add_node("analyze_media", analyze_media_node)
    graph.add_node("verify_candidates", verify_candidates_node)

    # 2. Define the entry point
    graph.set_entry_point("extract_media")

    # 3. Define the edges
    graph.add_edge("extract_media", "analyze_media")
    graph.add_edge("analyze_media", "verify_candidates")

    # 4. Define conditional edges (policies)
    graph.add_conditional_edges(
        "verify_candidates",
        should_verify_again,
        {
            "extract_media": "extract_media",  # Loop back if not enough evidence
            "end": END,
        },
    )

    # 5. Compile and return the graph
    return graph.compile()
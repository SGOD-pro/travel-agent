"""LangGraph workflow for direct media location analysis."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from .nodes import process_media_node
from .state import MediaLocationState


def create_media_location_graph() -> Any:
    """Create the media pipeline as one checkpoint-safe operation."""
    graph = StateGraph(MediaLocationState)
    graph.add_node("process_media", process_media_node)
    graph.add_edge(START, "process_media")
    graph.add_edge("process_media", END)
    return graph.compile()

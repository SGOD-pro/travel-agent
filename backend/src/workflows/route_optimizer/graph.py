"""Compiles the LangGraph workflow for the Route Optimizer."""
from typing import Any

from langgraph.graph import END, StateGraph

from .nodes import (
    constraint_node,
    geocode_node,
    routing_matrix_node,
    solver_node,
    validation_node,
)
from .policies import route_validation_policy
from .state import RouteState


def create_route_optimizer_graph() -> Any:
    graph = StateGraph(RouteState)

    graph.add_node("geocode", geocode_node)
    graph.add_node("routing_matrix", routing_matrix_node)
    graph.add_node("constraints", constraint_node)
    graph.add_node("solver", solver_node)
    graph.add_node("validation", validation_node)

    graph.set_entry_point("geocode")
    graph.add_edge("geocode", "routing_matrix")
    graph.add_edge("routing_matrix", "constraints")
    graph.add_edge("constraints", "solver")
    graph.add_edge("solver", "validation")

    graph.add_conditional_edges("validation", route_validation_policy, {
        "costs": END,
        "solver": "solver",
        "fail": END,
    })

    return graph.compile()

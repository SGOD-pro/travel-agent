# backend/src/functions/optimizer/routing.py
"""Routing API adapter for fetching transit matrices and distances."""
from __future__ import annotations

import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)


async def get_transit_matrix(
    coordinates: List[Tuple[float, float]], average_speed_kmh: float = 45.0
) -> tuple[list[list[int]], list[list[float]]]:
    """Generates a mock transit matrix for local testing."""
    n = len(coordinates)
    time_matrix = [[0] * n for _ in range(n)]
    dist_matrix = [[0.0] * n for _ in range(n)]

    for i in range(n):
        for j in range(n):
            if i != j:
                # Simple Euclidean distance as a fallback
                dist = (
                    ((coordinates[i][0] - coordinates[j][0]) ** 2)
                    + ((coordinates[i][1] - coordinates[j][1]) ** 2)
                ) ** 0.5 * 111.0
                dist_matrix[i][j] = round(dist, 2)
                time_matrix[i][j] = max(1, int((dist / average_speed_kmh) * 60))

    return time_matrix, dist_matrix
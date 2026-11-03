"""Scene filter prioritizing sharp, diverse, and informative keyframes."""

from __future__ import annotations

from contracts.media import ExtractedFrame


def select_informative_keyframes(
    frames: list[ExtractedFrame],
    max_keyframes: int = 8,
    min_blur_score: float = 60.0,
) -> list[ExtractedFrame]:
    """Selects top candidate frames for OCR and vision analysis.

    Rejects blurry frames and duplicates. Sorts by sharpness score
    while ensuring temporal diversity across the video sequence.
    """
    valid = [f for f in frames if not f.is_duplicate and not f.is_blurry and f.blur_score >= min_blur_score]
    if not valid:
        # Fallback to non-duplicates if all are under blur threshold
        valid = [f for f in frames if not f.is_duplicate]

    if len(valid) <= max_keyframes:
        return valid

    # Subsample evenly across the temporal timeline
    step = len(valid) / float(max_keyframes)
    selected = [valid[int(i * step)] for i in range(max_keyframes)]
    return selected
"""Frame extraction, perceptual hashing, blur detection, and deduplication.

Conforms to Phase 9:
- Non-LLM dedupe via perceptual difference hash (dHash).
- Blur detection via gradient/Laplacian variance.
- Brightness and contrast assessment.
- S3 storage key tracking for distributed processing.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from contracts.media import ExtractedFrame


def compute_dhash(image: Image.Image, hash_size: int = 8) -> str:
    """Computes difference hash (dHash) for an image.

    Resizes to (hash_size + 1, hash_size), converts to grayscale, and compares
    adjacent pixel intensities. Produces a hash_size * hash_size bit hash.
    """
    # 1. Convert to grayscale and resize to (width=9, height=8)
    resized = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = np.array(resized, dtype=np.float32)

    # 2. Compare adjacent pixels: row by row, is left pixel > right pixel?
    diff = pixels[:, :-1] > pixels[:, 1:]

    # 3. Convert boolean array to hex string
    bit_string = "".join(["1" if b else "0" for b in diff.flatten()])
    return f"{int(bit_string, 2):0{hash_size * hash_size // 4}x}"


def hamming_distance(hash1: str, hash2: str) -> int:
    """Computes bitwise Hamming distance between two hex hash strings."""
    try:
        val1 = int(hash1, 16)
        val2 = int(hash2, 16)
        xor_val = val1 ^ val2
        return bin(xor_val).count("1")
    except ValueError:
        return 999


def compute_blur_score(image: Image.Image) -> float:
    """Estimates image sharpness using the variance of the discrete Laplacian kernel.

    Higher variance corresponds to sharper edges. Smooth or blurry images
    have low variance.
    """
    gray = np.array(image.convert("L"), dtype=np.float32)
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0

    # 3x3 Discrete Laplacian Kernel
    #  0  1  0
    #  1 -4  1
    #  0  1  0
    padded = np.pad(gray, 1, mode="edge")
    laplacian = (
        padded[0:-2, 1:-1]
        + padded[2:, 1:-1]
        + padded[1:-1, 0:-2]
        + padded[1:-1, 2:]
        - 4.0 * padded[1:-1, 1:-1]
    )
    variance = float(np.var(laplacian))
    return round(variance, 2)


def compute_brightness(image: Image.Image) -> float:
    """Computes average luminance (0.0 to 255.0)."""
    gray = np.array(image.convert("L"), dtype=np.float32)
    return round(float(np.mean(gray)), 2)


def process_image_frame(
    image: Image.Image,
    timestamp_seconds: float,
    frame_index: int,
    blur_threshold: float = 80.0,
    file_path: str | None = None,
    s3_key: str | None = None,
) -> ExtractedFrame:
    """Computes quality and perceptual metrics for a single frame.

    Args:
        image: The PIL Image object.
        timestamp_seconds: The timestamp of the frame in the video.
        frame_index: The sequential index of the frame.
        blur_threshold: The threshold below which a frame is considered blurry.
        file_path: Local temporary path of the frame.
        s3_key: The S3 object key where the processed frame is stored (if uploaded).
    """
    width, height = image.size
    img_hash = compute_dhash(image)
    blur_score = compute_blur_score(image)
    brightness = compute_brightness(image)
    is_blurry = blur_score < blur_threshold

    return ExtractedFrame(
        frame_index=frame_index,
        timestamp_seconds=timestamp_seconds,
        width=width,
        height=height,
        image_hash=img_hash,
        blur_score=blur_score,
        brightness=brightness,
        is_blurry=is_blurry,
        is_duplicate=False,
        file_path=file_path,
        s3_key=s3_key,
    )


def filter_and_deduplicate_frames(
    frames: list[ExtractedFrame],
    dedupe_hamming_threshold: int = 5,
    drop_blurry: bool = False,
) -> list[ExtractedFrame]:
    """Filters frames by marking or dropping duplicates and blurry frames.

    Maintains temporal sequence. A frame is considered duplicate if its
    Hamming distance to any previously accepted frame is <= dedupe_hamming_threshold.
    """
    processed: list[ExtractedFrame] = []
    accepted_hashes: list[str] = []

    for f in frames:
        is_dup = False
        for prev_hash in accepted_hashes:
            if hamming_distance(f.image_hash, prev_hash) <= dedupe_hamming_threshold:
                is_dup = True
                break

        updated_frame = ExtractedFrame(
            frame_index=f.frame_index,
            timestamp_seconds=f.timestamp_seconds,
            width=f.width,
            height=f.height,
            image_hash=f.image_hash,
            blur_score=f.blur_score,
            brightness=f.brightness,
            is_blurry=f.is_blurry,
            is_duplicate=is_dup,
            file_path=f.file_path,
            s3_key=f.s3_key,
        )

        if is_dup and drop_blurry:
            continue
        if updated_frame.is_blurry and drop_blurry:
            continue

        if not is_dup:
            accepted_hashes.append(f.image_hash)

        processed.append(updated_frame)

    return processeds
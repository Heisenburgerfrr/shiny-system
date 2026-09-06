"""Cover image loader, validator, and Instagram Reels compliance checker."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger("bot.cover")

# Meta / Instagram Reels Technical Specifications
REELS_TARGET_ASPECT_RATIO = 9 / 16  # 0.5625
ASPECT_RATIO_TOLERANCE = 0.04       # 0.5225 - 0.6025
MIN_RECOMMENDED_WIDTH = 500         # Meta recommended minimum width
RECOMMENDED_RESOLUTION = (1080, 1920)
MAX_FILE_SIZE_BYTES = 8 * 1024 * 1024  # 8 MB Meta limit
SUPPORTED_FORMATS = {"JPEG", "PNG"}


@dataclass
class CoverValidationResult:
    """Detailed results of cover image inspection against Instagram Reels specs."""
    is_valid: bool
    status_summary: str
    details: str
    width: int
    height: int
    format: str
    file_size: int
    file_path: Path


def inspect_cover_image(cover_path: Path) -> CoverValidationResult:
    """
    Inspects and validates an image file against Instagram Reels cover specifications.
    Does NOT auto-resize or alter the file.
    """
    path = Path(cover_path).resolve()

    if not path.exists():
        return CoverValidationResult(
            is_valid=False,
            status_summary="Missing",
            details=f"Cover file does not exist at {path}",
            width=0,
            height=0,
            format="UNKNOWN",
            file_size=0,
            file_path=path,
        )

    file_size = path.stat().st_size
    if file_size == 0:
        return CoverValidationResult(
            is_valid=False,
            status_summary="Empty file",
            details=f"Cover file is empty (0 bytes): {path}",
            width=0,
            height=0,
            format="UNKNOWN",
            file_size=0,
            file_path=path,
        )

    if file_size > MAX_FILE_SIZE_BYTES:
        return CoverValidationResult(
            is_valid=False,
            status_summary="Too large",
            details=f"Cover file exceeds 8 MB Meta limit ({file_size / (1024*1024):.2f} MB)",
            width=0,
            height=0,
            format="UNKNOWN",
            file_size=file_size,
            file_path=path,
        )

    try:
        with Image.open(path) as img:
            img_format = (img.format or "UNKNOWN").upper()
            width, height = img.size

            if img_format not in SUPPORTED_FORMATS:
                return CoverValidationResult(
                    is_valid=False,
                    status_summary="Unsupported format",
                    details=f"Format '{img_format}' is not supported by Instagram. Must be JPEG or PNG.",
                    width=width,
                    height=height,
                    format=img_format,
                    file_size=file_size,
                    file_path=path,
                )

            ratio = width / height if height > 0 else 0.0
            is_9_16 = abs(ratio - REELS_TARGET_ASPECT_RATIO) <= ASPECT_RATIO_TOLERANCE

            if not is_9_16:
                return CoverValidationResult(
                    is_valid=False,
                    status_summary="Invalid aspect ratio",
                    details=(
                        f"Dimensions {width}x{height} have aspect ratio {ratio:.2f}:1. "
                        f"Instagram Reels requires 9:16 (0.56:1, e.g. 1080x1920)."
                    ),
                    width=width,
                    height=height,
                    format=img_format,
                    file_size=file_size,
                    file_path=path,
                )

            # Valid 9:16 aspect ratio!
            size_kb = file_size / 1024.0
            res_str = f"{width}x{height}"
            details = f"{res_str} ({img_format}, 9:16, {size_kb:.1f} KB)"
            if width < MIN_RECOMMENDED_WIDTH:
                details += f" [Note: Meta recommends 1080x1920 for maximum sharpness]"

            return CoverValidationResult(
                is_valid=True,
                status_summary="Valid Reels 9:16 Cover",
                details=details,
                width=width,
                height=height,
                format=img_format,
                file_size=file_size,
                file_path=path,
            )

    except UnidentifiedImageError:
        return CoverValidationResult(
            is_valid=False,
            status_summary="Unreadable image",
            details=f"File {path.name} is not a valid or readable image.",
            width=0,
            height=0,
            format="UNKNOWN",
            file_size=file_size,
            file_path=path,
        )
    except Exception as exc:
        return CoverValidationResult(
            is_valid=False,
            status_summary="Error inspecting image",
            details=f"Exception reading cover: {exc}",
            width=0,
            height=0,
            format="UNKNOWN",
            file_size=file_size,
            file_path=path,
        )


def crop_and_save_cover_image(
    input_path: Path,
    output_path: Path,
    target_resolution: Tuple[int, int] = RECOMMENDED_RESOLUTION,
) -> CoverValidationResult:
    """
    Opens any image, auto-orients EXIF, center-crops to 9:16 aspect ratio,
    resizes to target_resolution (1080x1920), converts to RGB JPEG, and saves to output_path.
    Returns the CoverValidationResult of the saved image.
    """
    from PIL import ImageOps

    input_p = Path(input_path).resolve()
    output_p = Path(output_path).resolve()

    if not input_p.is_file():
        raise FileNotFoundError(f"Source cover image not found at {input_p}")

    with Image.open(input_p) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        w, h = img.size

        target_ratio = REELS_TARGET_ASPECT_RATIO
        current_ratio = w / h if h > 0 else target_ratio

        if current_ratio > target_ratio:
            # Too wide: crop horizontal sides
            crop_w = int(round(h * target_ratio))
            left = max(0, (w - crop_w) // 2)
            img = img.crop((left, 0, left + crop_w, h))
        elif current_ratio < target_ratio:
            # Too tall: crop top and bottom
            crop_h = int(round(w / target_ratio))
            top = max(0, (h - crop_h) // 2)
            img = img.crop((0, top, w, top + crop_h))

        img = img.resize(target_resolution, Image.Resampling.LANCZOS)
        output_p.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_p, format="JPEG", quality=95, optimize=True)

    return inspect_cover_image(output_p)

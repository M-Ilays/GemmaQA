"""Cropped-screenshot preparation for targeted visual analysis.

Reuses the risky-screenshot-name skip already established in
`app.gemma.images` rather than duplicating that policy — a crop of a
credential-adjacent screenshot must be skipped for the same reason the full
screenshot is. Cropping itself is a plain, local Pillow operation: nothing
here calls a model or the browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.gemma.images import should_skip_screenshot
from app.utils.logging import get_logger

logger = get_logger("perception.screenshot_utils")

# Padding gives the visual model a little surrounding context rather than a
# pixel-exact crop of just the element's own box.
DEFAULT_PADDING_PX = 12
CROP_SUBDIR = "crops"


def crop_bounding_box(
    screenshot_path: str | Path,
    bounding_box: dict[str, float],
    *,
    element_id: str,
    out_dir: str | Path | None = None,
    padding: int = DEFAULT_PADDING_PX,
) -> Optional[str]:
    """Crop one element's region out of a full-page screenshot and save it
    next to the source screenshot (or under `out_dir` if given).

    Returns the crop's path on success, or None on ANY failure — a failed
    crop degrades to skipping that target (or falling back to the full
    screenshot), never fatal to the observation loop."""
    if not bounding_box:
        return None
    try:
        from PIL import Image
    except Exception:
        logger.info("Pillow unavailable; skipping cropped screenshot for %s", element_id)
        return None

    try:
        src = Path(screenshot_path)
        if not src.exists() or not src.is_file() or should_skip_screenshot(src):
            return None

        width = float(bounding_box.get("width", 0) or 0)
        height = float(bounding_box.get("height", 0) or 0)
        if width <= 0 or height <= 0:
            return None

        with Image.open(src) as img:
            img_w, img_h = img.size
            x0 = max(0, int(bounding_box.get("x", 0)) - padding)
            y0 = max(0, int(bounding_box.get("y", 0)) - padding)
            x1 = min(img_w, x0 + int(width) + padding * 2)
            y1 = min(img_h, y0 + int(height) + padding * 2)
            if x1 <= x0 or y1 <= y0:
                return None
            cropped = img.crop((x0, y0, x1, y1))

            target_dir = Path(out_dir) if out_dir else src.parent / CROP_SUBDIR
            target_dir.mkdir(parents=True, exist_ok=True)
            out_path = target_dir / f"{src.stem}__{element_id}.png"
            cropped.save(out_path)
            return str(out_path)
    except Exception as exc:
        logger.warning("Failed to crop screenshot for %s: %s", element_id, type(exc).__name__)
        return None

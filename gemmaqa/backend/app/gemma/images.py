"""Screenshot preparation for multimodal Gemma calls."""

from __future__ import annotations

import base64
import io
from pathlib import Path

from app.utils.logging import get_logger

logger = get_logger("gemma.images")

# Skip paths that are likely to show typed credentials even after masking attempts
RISKY_NAME_FRAGMENTS = ("login_failed", "password", "passwd", "credentials")


def should_skip_screenshot(path: str | Path | None) -> bool:
    if not path:
        return True
    name = Path(path).name.lower()
    return any(frag in name for frag in RISKY_NAME_FRAGMENTS)


def prepare_image_data_url(
    path: str | Path,
    *,
    max_side: int = 1280,
    max_bytes: int = 700_000,
) -> str | None:
    """
    Load a screenshot, optionally resize/compress, return a data URL.
    Returns None on failure (caller should continue text-only).
    """
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None
        if should_skip_screenshot(p):
            logger.info("Skipping risky screenshot for model: %s", p.name)
            return None

        raw = p.read_bytes()
        mime = "image/png"
        suffix = p.suffix.lower()

        # Try Pillow resize/compress when available; otherwise send original if small enough
        try:
            from PIL import Image  # type: ignore

            img = Image.open(io.BytesIO(raw))
            img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
            w, h = img.size
            scale = min(1.0, max_side / max(w, h))
            if scale < 1.0:
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            buf = io.BytesIO()
            # Prefer JPEG for size when not tiny PNG
            img.save(buf, format="JPEG", quality=72, optimize=True)
            data = buf.getvalue()
            mime = "image/jpeg"
            # If still too large, shrink further
            quality = 60
            while len(data) > max_bytes and quality >= 35:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True)
                data = buf.getvalue()
                quality -= 10
        except Exception:
            data = raw
            if suffix in {".jpg", ".jpeg"}:
                mime = "image/jpeg"
            elif suffix == ".webp":
                mime = "image/webp"
            else:
                mime = "image/png"
            if len(data) > max_bytes:
                logger.warning(
                    "Screenshot too large (%s bytes) and Pillow unavailable; omitting image",
                    len(data),
                )
                return None

        if len(data) > max_bytes:
            logger.warning("Screenshot still too large after compress; omitting image")
            return None

        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as exc:
        logger.warning("Failed to prepare screenshot: %s", type(exc).__name__)
        return None

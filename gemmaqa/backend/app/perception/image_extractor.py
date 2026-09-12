"""Image extraction — builds `ImageDescriptor` from the raw image inventory
`dom_extractor` collected. Direct fix for docs/PAGE_PERCEPTION_AUDIT.md's
"no general image inventory" finding (previously `<img alt>` was read only as
a fallback for one icon-only control's accessible name, never as its own
first-class data point).

Also classifies every image into one of the 8 generic semantic categories the
Visual Observation Policy expects (decorative/informational/interactive/
document/chart/avatar/logo/unknown) — deterministically, from DOM facts alone
(alt text, src, class name, container, bounding box). No LLM call here: this
is a cheap first pass so the (comparatively expensive, non-deterministic)
visual model is only ever asked to look at "meaningful" images, never every
`<img>` on the page. See docs/VISUAL_OBSERVATION_POLICY.md.
"""

from __future__ import annotations

import re

from app.perception.models import ImageDescriptor

# Images classified into these categories carry enough DOM/accessibility
# evidence on their own; the visual model is never asked to look at them.
NON_MEANINGFUL_IMAGE_TYPES = frozenset({"decorative", "avatar", "logo"})
MEANINGFUL_IMAGE_TYPES = frozenset(
    {"informational", "interactive", "document", "chart", "unknown"}
)

_LOGO_RE = re.compile(r"\blogo\b", re.IGNORECASE)
_AVATAR_RE = re.compile(r"\b(avatar|profile[-_ ]?(pic|photo|image)?|headshot|user[-_ ]?photo)\b", re.IGNORECASE)
_CHART_RE = re.compile(r"\b(chart|graph|plot|sparkline|histogram)\b", re.IGNORECASE)
_DOCUMENT_RE = re.compile(r"\b(document|scan(ned)?|receipt|invoice-image|contract|statement)\b", re.IGNORECASE)
_PDF_SRC_RE = re.compile(r"\.pdf(\?|#|$)", re.IGNORECASE)

_SMALL_SIDE_MAX = 160.0
_TINY_SIDE_MAX = 32.0


def classify_image_semantic(img: dict, *, bounding_box: dict | None) -> str:
    """Deterministic, heuristic-only classification into one of
    `IMAGE_SEMANTIC_TYPES`. Order matters — most specific signals first."""
    alt_raw = img.get("alt")
    alt = (alt_raw or "").strip()
    src = (img.get("src") or "")
    class_name = (img.get("class_name") or "")
    role = (img.get("role") or "").lower()
    surrounding = (img.get("surrounding_text") or "").strip()
    haystack = " ".join([alt, src, class_name])

    box = bounding_box or {}
    width = float(box.get("width", 0) or 0)
    height = float(box.get("height", 0) or 0)
    is_small = 0 < width <= _SMALL_SIDE_MAX and 0 < height <= _SMALL_SIDE_MAX
    is_tiny = 0 < width <= _TINY_SIDE_MAX and 0 < height <= _TINY_SIDE_MAX
    is_square_ish = width > 0 and height > 0 and 0.7 <= (width / height) <= 1.4

    if _LOGO_RE.search(haystack):
        return "logo"
    if _AVATAR_RE.search(haystack) and is_small and is_square_ish:
        return "avatar"
    if alt_raw == "" or role in {"presentation", "none"}:
        # An explicitly empty alt attribute (as opposed to a missing one) is
        # the standard HTML signal that an image is purely decorative.
        return "decorative"
    if _CHART_RE.search(haystack) or _CHART_RE.search(surrounding):
        return "chart"
    if _PDF_SRC_RE.search(src) or _DOCUMENT_RE.search(haystack):
        return "document"
    if img.get("in_interactive_container"):
        return "interactive"
    if is_tiny:
        # A tiny image with no alt/role signal and no meaningful surrounding
        # text is most likely an icon glyph, not content worth analyzing.
        return "unknown"
    has_descriptive_alt = len(alt.split()) >= 2
    has_descriptive_context = len(surrounding) >= 40
    if has_descriptive_alt or has_descriptive_context:
        return "informational"
    return "unknown"


def extract_images(raw) -> list[ImageDescriptor]:  # raw: RawObservation
    images: list[ImageDescriptor] = []
    for img in raw.images:
        dom_id = img.get("dom_id")
        if not dom_id:
            continue
        alt = img.get("alt")
        bounding_box = img.get("bounding_box")
        semantic_type = classify_image_semantic(img, bounding_box=bounding_box)
        images.append(
            ImageDescriptor(
                element_id=dom_id,
                stable_id=dom_id,
                accessible_name=alt or None,
                alt_text=alt,
                src=img.get("src"),
                surrounding_text=img.get("surrounding_text"),
                bounding_box=bounding_box,
                visual_semantic_type=semantic_type,
                source="dom",
                confidence=1.0 if alt else 0.6,
            )
        )
    return images

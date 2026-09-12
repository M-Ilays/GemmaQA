"""Safe file-upload fixtures — the only files GemmaQA is ever allowed to
upload during a run. Every fixture here is a small, static, repository-
controlled file checked into GemmaQA's own codebase; nothing is generated
on the fly and nothing is ever sourced from the target application, the
run's own captured evidence, or user input. This directly satisfies "file
upload using repository-controlled safe fixtures only. Do not generate
arbitrary files."
"""

from __future__ import annotations

from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "safe_uploads"

SAFE_UPLOAD_FIXTURES: dict[str, Path] = {
    "text": FIXTURES_DIR / "gemmaqa_test_upload.txt",
    "image": FIXTURES_DIR / "gemmaqa_test_image.png",
}

_IMAGE_ACCEPT_HINTS = ("image", ".png", ".jpg", ".jpeg", ".gif", ".webp")


def default_safe_upload_fixture(*, accept: str | None = None) -> Path:
    """Picks the best-fitting fixture for an `<input type="file" accept=...>`
    hint. Falls back to the plain-text fixture whenever `accept` doesn't
    clearly call for an image — text is the safest, most broadly accepted
    default. Never returns a path outside `SAFE_UPLOAD_FIXTURES`."""
    accept_lower = (accept or "").lower()
    if any(hint in accept_lower for hint in _IMAGE_ACCEPT_HINTS):
        return SAFE_UPLOAD_FIXTURES["image"]
    return SAFE_UPLOAD_FIXTURES["text"]

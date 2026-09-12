"""Canonical application structure package."""

from app.application.coverage import compute_coverage
from app.application.models import ApplicationModel
from app.application.store import ApplicationStore
from app.application.url_normalize import normalize_url, origin_of, same_origin

__all__ = [
    "ApplicationModel",
    "ApplicationStore",
    "compute_coverage",
    "normalize_url",
    "origin_of",
    "same_origin",
]

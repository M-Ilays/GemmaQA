"""Deterministic URL normalization for application structure."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

# Tracking / analytics params that must not create duplicate pages
_DROP_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "_gl",
    "ref",
    "source",
}


def origin_of(url: str) -> str:
    """scheme + hostname + effective port (omit default ports)."""
    parsed = urlparse((url or "").strip())
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    port = parsed.port
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def same_origin(url: str, allowed_origin: str) -> bool:
    if not url or not allowed_origin:
        return False
    return origin_of(url) == origin_of(allowed_origin)


def normalize_url(url: str, *, base_url: str | None = None) -> str:
    """
    Canonical absolute URL:
    - resolve relative against base
    - drop fragment
    - drop tracking query params
    - normalize trailing slash (keep root `/`, strip elsewhere)
    - lowercase scheme/host
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if base_url and not raw.startswith(("http://", "https://")):
        raw = urljoin(base_url, raw)
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return raw.split("#", 1)[0]

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _DROP_QUERY_KEYS
    ]
    query_pairs.sort(key=lambda kv: (kv[0].lower(), kv[1]))
    query = urlencode(query_pairs, doseq=True)

    return urlunparse((scheme, netloc, path, "", query, ""))


def normalized_path(url: str) -> str:
    parsed = urlparse(normalize_url(url) or url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def is_external_doc_or_social(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    markers = (
        "postman.com",
        "getpostman.com",
        "documenter.getpostman.com",
        "github.com",
        "gitlab.com",
        "twitter.com",
        "x.com",
        "facebook.com",
        "linkedin.com",
        "youtube.com",
        "wikipedia.org",
        "stackoverflow.com",
        "notion.so",
        "docs.google.com",
    )
    return any(host == m or host.endswith("." + m) for m in markers)

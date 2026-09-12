"""URL parsing, normalization, and target authorization guards."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse, urlunparse

# Schemes never allowed as navigation / test targets
FORBIDDEN_SCHEMES = frozenset(
    {
        "file",
        "javascript",
        "data",
        "blob",
        "about",
        "chrome",
        "chrome-extension",
        "edge",
        "moz-extension",
        "view-source",
        "devtools",
        "ws",
        "wss",
        "ftp",
        "ftps",
    }
)

BROWSER_INTERNAL_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "broadcasthost",
        "metadata.google.internal",
        "metadata",
        "kubernetes.default",
        "kubernetes.default.svc",
    }
)

# Static CDNs — network loads OK; never treat as interactive test targets
DEFAULT_CDN_HOST_SUFFIXES: tuple[str, ...] = (
    "cdn.jsdelivr.net",
    "cdnjs.cloudflare.com",
    "unpkg.com",
    "ajax.googleapis.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "static.cloudflareinsights.com",
    "cdn.tailwindcss.com",
)

# Cloud metadata / link-local specials
METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
)

_INTERNAL_HOSTNAME_RE = re.compile(
    r"(^|\.)(local|internal|intranet|corp|lan|home|private)(\.|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NormalizedUrl:
    original: str
    normalized: str
    scheme: str
    hostname: str
    port: int | None
    path: str


@dataclass(frozen=True)
class UrlValidationResult:
    ok: bool
    reason: str = ""
    normalized: NormalizedUrl | None = None


def normalize_url(url: str) -> NormalizedUrl:
    """Parse and normalize a user-supplied URL."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    # Drop userinfo from normalized form
    netloc = hostname
    if parsed.port:
        netloc = f"{hostname}:{parsed.port}"
    path = parsed.path or "/"
    normalized = urlunparse((scheme, netloc, path, "", parsed.query, ""))
    return NormalizedUrl(
        original=raw,
        normalized=normalized,
        scheme=scheme,
        hostname=hostname,
        port=parsed.port,
        path=path,
    )


def _is_private_or_local_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_local_or_internal_host(hostname: str, *, allow_local: bool = False) -> bool:
    """Return True if hostname is local/private/metadata (blocked when allow_local is False)."""
    host = (hostname or "").lower().rstrip(".")
    if not host:
        return True
    if host in METADATA_HOSTS or host in BROWSER_INTERNAL_HOSTS:
        return True
    if host.endswith(".local") or host.endswith(".internal") or host.endswith(".localhost"):
        return True
    if _INTERNAL_HOSTNAME_RE.search(host):
        return True
    if _is_private_or_local_ip(host):
        return True
    # Literal localhost variants
    if host in {"localhost", "0.0.0.0", "::1"}:
        return True
    return False


def is_cdn_host(hostname: str, cdn_suffixes: Iterable[str] | None = None) -> bool:
    host = (hostname or "").lower()
    suffixes = tuple(cdn_suffixes or DEFAULT_CDN_HOST_SUFFIXES)
    return any(host == s or host.endswith(f".{s}") for s in suffixes)


def validate_target_url(
    url: str,
    *,
    allow_local_targets: bool = False,
    require_https_for_credentials: bool = False,
    has_credentials: bool = False,
) -> UrlValidationResult:
    """Validate a create-run / navigation target URL."""
    if not url or not str(url).strip():
        return UrlValidationResult(False, "URL is required")

    raw = str(url).strip()
    lower = raw.lower()

    # Explicit forbidden scheme prefixes (even before parse quirks)
    for scheme in FORBIDDEN_SCHEMES:
        if lower.startswith(f"{scheme}:"):
            return UrlValidationResult(False, f"Scheme '{scheme}' is not allowed")

    try:
        norm = normalize_url(raw)
    except Exception:
        return UrlValidationResult(False, "URL could not be parsed")

    if norm.scheme not in {"http", "https"}:
        return UrlValidationResult(
            False, "URL must use http:// or https:// (file/javascript/data/browser URLs are rejected)"
        )

    if not norm.hostname:
        return UrlValidationResult(False, "URL must include a hostname")

    # Reject embedded credentials in the URL itself
    parsed = urlparse(raw)
    if parsed.username or parsed.password:
        return UrlValidationResult(False, "Credentials in the URL are not allowed")

    if norm.hostname in METADATA_HOSTS or norm.hostname == "169.254.169.254":
        return UrlValidationResult(False, "Cloud metadata endpoints are blocked")

    if is_local_or_internal_host(norm.hostname) and not allow_local_targets:
        return UrlValidationResult(
            False,
            "Local/internal network targets are blocked "
            "(set ALLOW_LOCAL_TARGETS=true for demo/development only)",
        )

    if has_credentials and require_https_for_credentials and norm.scheme != "https":
        return UrlValidationResult(
            False,
            "Credentials require an HTTPS target URL in deployed environments",
        )

    return UrlValidationResult(True, "ok", normalized=norm)


def host_in_scope(
    candidate_url: str,
    *,
    authorized_hostname: str,
    allow_subdomains: bool = False,
    allow_cross_domain: bool = False,
    cdn_suffixes: Iterable[str] | None = None,
) -> tuple[bool, str]:
    """
    Decide whether a candidate URL is an allowed *test target*.

    Relative URLs (no scheme/host) stay in scope.
    CDN hosts are never interactive test targets (loads may still happen in the browser).
    """
    if allow_cross_domain:
        return True, "cross_domain_allowed"

    try:
        parsed = urlparse(candidate_url)
    except Exception:
        return False, "unparseable_url"

    scheme = (parsed.scheme or "").lower()
    if scheme in FORBIDDEN_SCHEMES:
        return False, f"forbidden_scheme:{scheme}"

    if not scheme or not parsed.hostname:
        # Relative / same-document
        return True, "relative"

    if scheme not in {"http", "https"}:
        return False, f"forbidden_scheme:{scheme}"

    host = parsed.hostname.lower().rstrip(".")
    auth = (authorized_hostname or "").lower().rstrip(".")

    if is_cdn_host(host, cdn_suffixes):
        return False, "cdn_not_a_test_target"

    if not auth:
        return False, "no_authorized_domain"

    if host == auth:
        return True, "exact_host"

    if allow_subdomains and host.endswith(f".{auth}"):
        return True, "subdomain"

    return False, "external_domain_blocked"


def is_cross_domain_embed(url: str, authorized_hostname: str) -> bool:
    """True when URL points at a different host (including CDN embeds)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return True
    if not host:
        return False
    auth = (authorized_hostname or "").lower()
    return host != auth and not host.endswith(f".{auth}")

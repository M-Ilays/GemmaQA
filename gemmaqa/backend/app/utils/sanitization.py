"""Sanitization helpers for logs, credentials, and evidence metadata."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

SENSITIVE_KEYS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apikey",
    "secret",
    "authorization",
    "cookie",
    "set-cookie",
    "credentials",
    "session",
    "sessionid",
    "session_id",
    "csrf",
    "xsrf",
    "private_key",
    "client_secret",
    "auth",
    # Payment and government identifiers. Not credentials, but they must never
    # reach a model prompt, a log, or a report either — and a QA agent filling
    # test forms will encounter fields named exactly these.
    "card_number",
    "cardnumber",
    "card_num",
    "cvv",
    "cvc",
    "card_security_code",
    "iban",
    "account_number",
    "routing_number",
    "sort_code",
    "ssn",
    "social_security",
    "tax_id",
    "passport",
    "national_id",
}

SENSITIVE_QUERY_PARAMS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "key",
    "secret",
    "auth",
    "session",
    "sessionid",
    "sid",
    "jwt",
    "code",
    "signature",
}

MASK = "***REDACTED***"

# Value-level patterns (Bearer tokens, cookie headers, PEM-ish blobs)
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(bearer\s+)[a-z0-9._\-+=/]+"),
    re.compile(r"(?i)(basic\s+)[a-z0-9=+/]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(password\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(sessionid\s*[:=]\s*)\S+"),
    re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*)[^\n;]+"),
)


def mask_secret(value: str | None) -> str:
    """Mask a secret value for safe logging / DB display."""
    if value is None or value == "":
        return ""
    if len(value) <= 4:
        return MASK
    return f"{value[:2]}{MASK}{value[-1]}"


# Candidate payment-card numbers: 13-19 digits, optionally grouped by spaces or
# dashes. Matched loosely here and then confirmed by a Luhn check, because the
# same shape is also a perfectly ordinary order id or row count — masking every
# long number would strip real observations a QA agent needs.
_PAN_CANDIDATE = re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")


def _is_luhn_valid(digits: str) -> bool:
    if not 13 <= len(digits) <= 19 or not digits.isdigit():
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _mask_payment_cards(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        digits = re.sub(r"[ -]", "", match.group(0))
        return MASK if _is_luhn_valid(digits) else match.group(0)

    return _PAN_CANDIDATE.sub(_replace, text)


def sanitize_text(value: str | None) -> str:
    """Redact secret-shaped substrings from free text / error messages."""
    if value is None:
        return ""
    text = str(value)
    for pattern in _VALUE_PATTERNS:
        text = pattern.sub(rf"\1{MASK}", text)
    return _mask_payment_cards(text)


def sanitize_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively mask sensitive keys in a dictionary."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        lower = str(key).lower().replace("-", "_")
        if any(s in lower for s in SENSITIVE_KEYS):
            result[key] = MASK if value not in (None, "") else value
        elif isinstance(value, dict):
            result[key] = sanitize_dict(value)
        elif isinstance(value, list):
            result[key] = [
                sanitize_dict(item)
                if isinstance(item, dict)
                else sanitize_text(item)
                if isinstance(item, str)
                else item
                for item in value
            ]
        elif isinstance(value, str):
            result[key] = sanitize_text(value)
        else:
            result[key] = value
    return result


def sanitize_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    """Mask Authorization, Cookie, and other sensitive headers."""
    if not headers:
        return {}
    out: dict[str, str] = {}
    for key, value in headers.items():
        lower = str(key).lower()
        if any(s in lower.replace("-", "_") for s in SENSITIVE_KEYS) or lower in {
            "authorization",
            "cookie",
            "set-cookie",
            "x-api-key",
            "x-auth-token",
        }:
            out[str(key)] = MASK
        else:
            out[str(key)] = sanitize_text(str(value))
    return out


def sanitize_cookies(cookies: dict[str, Any] | list[dict[str, Any]] | None) -> Any:
    """Redact cookie values while preserving names for debugging."""
    if cookies is None:
        return None
    if isinstance(cookies, dict):
        return {str(k): MASK for k in cookies}
    if isinstance(cookies, list):
        sanitized: list[dict[str, Any]] = []
        for item in cookies:
            if isinstance(item, dict):
                copy = dict(item)
                if "value" in copy:
                    copy["value"] = MASK
                sanitized.append(sanitize_dict(copy))
            else:
                sanitized.append({"value": MASK})
        return sanitized
    return MASK


def sanitize_query_params(params: dict[str, Any] | None) -> dict[str, Any]:
    if not params:
        return {}
    out: dict[str, Any] = {}
    for key, value in params.items():
        if str(key).lower() in SENSITIVE_QUERY_PARAMS or any(
            s in str(key).lower() for s in SENSITIVE_KEYS
        ):
            out[key] = MASK
        else:
            out[key] = value
    return out


def sanitize_request_body(body: Any) -> Any:
    if body is None:
        return None
    if isinstance(body, dict):
        return sanitize_dict(body)
    if isinstance(body, str):
        return sanitize_text(body)
    if isinstance(body, bytes):
        try:
            return sanitize_text(body.decode("utf-8", errors="replace"))
        except Exception:
            return MASK
    return body


def sanitize_local_storage(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Redact storage snapshots (never persist secrets)."""
    if not snapshot:
        return {}
    return {str(k): MASK for k in snapshot}


def sanitize_url(url: str) -> str:
    """Strip credentials and sensitive query parameters from a URL."""
    try:
        parsed = urlparse(url)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        # Drop userinfo always
        query_pairs = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() in SENSITIVE_QUERY_PARAMS or any(
                s in key.lower() for s in SENSITIVE_KEYS
            ):
                query_pairs.append((key, MASK))
            else:
                query_pairs.append((key, value))
        return urlunparse(
            (
                parsed.scheme,
                netloc,
                parsed.path,
                parsed.params,
                urlencode(query_pairs),
                parsed.fragment,
            )
        )
    except Exception:
        return url


def truncate_text(text: str, max_len: int = 500) -> str:
    """Truncate long text for summaries."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def clamp_prompt(text: str, max_chars: int) -> str:
    """Enforce maximum prompt size."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    suffix = "\n...[prompt truncated]..."
    if max_chars <= len(suffix):
        return text[:max_chars]
    return text[: max_chars - len(suffix)] + suffix

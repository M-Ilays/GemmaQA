"""A form's identity, independent of where it sits in the document.

`FormDescriptor.form_id` is NOT an identity. It comes from `nextId('form')` in
`app.browser.observer`'s injected script — a document-order counter shared with
element ids — so it changes whenever anything before the form in the DOM
changes. Including, fatally, changes the form's own submit causes.

Observed live on OrangeHRM's Buzz newsfeed: the post box sits *below* the feed,
so every accepted post inserted an entry above it and renumbered the form
(`form_055` -> `form_056` -> `form_085`). Every per-form guard keyed on that id
reset each time, and the run posted 20 times in 44 actions before the operator
cancelled it.

This is the same defect class as #167, where a navigation item was signed by a
page fingerprint that clicking it changed. The rule earned there applies here:
**never identify a thing by something the action on it modifies.**

One implementation, imported by the frontier, the planner, and the controller —
three call sites that must agree, and the session that produced this module lost
hours to four bugs where a rule had two homes and only one was on the measured
path.
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit

# Enough of the shape to tell two different forms apart, short enough to read in
# a log line.
_DIGEST_CHARS = 12

# How many fields contribute. A form with hundreds of inputs is not made more
# distinguishable by the tail of them, and the cap keeps the cost flat.
_MAX_FIELDS = 40


def _field_token(field: Any) -> str:
    """What this field is, in terms that survive the page being re-rendered.

    Prefers the identifiers an application chooses deliberately (`name`, a
    label) over ones the browser or the extractor assigns positionally
    (`element_id`), which carry the same defect as `form_id`.
    """
    for attribute in ("name", "label", "placeholder"):
        value = (getattr(field, attribute, None) or "").strip().lower()
        if value:
            return f"{attribute}:{value}"
    # Nothing nameable — OrangeHRM's Buzz box is exactly this: a bare textarea
    # with no name, no label and no placeholder. The type alone still separates
    # it from a text input beside it, and the URL below does the rest.
    return f"type:{(getattr(field, 'field_type', None) or 'text').strip().lower()}"


def form_signature(form: Any, page_url: str = "") -> str:
    """A stable id for `form` on the page at `page_url`.

    Two forms match when they are on the same path and present the same fields
    in the same order. Query strings and fragments are excluded: a list filtered
    to `?page=2` offers the same create form as `?page=1`, and treating those as
    different forms would re-open the loop this function exists to close.
    """
    parts = urlsplit(page_url or "")
    tokens = [f"path:{parts.netloc}{parts.path}"]
    for field in list(getattr(form, "fields", None) or [])[:_MAX_FIELDS]:
        tokens.append(_field_token(field))
    digest = hashlib.sha256("|".join(tokens).encode("utf-8")).hexdigest()
    return f"form:{digest[:_DIGEST_CHARS]}"

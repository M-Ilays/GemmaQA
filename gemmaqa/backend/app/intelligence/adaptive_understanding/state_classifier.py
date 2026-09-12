"""Classifies "what am I looking at?" from STRUCTURAL EVIDENCE ONLY.

This module never reads the URL. Not the path, not the query string, not the
hostname. That is a deliberate, load-bearing constraint, for three reasons:

1. **URLs lie.** A single-page application can present a login screen, a
   dashboard, and a modal at the same URL. A REST-shaped URL can render an
   error page. A route named `/create` can show a permission denial.
2. **URLs are application-specific.** Matching `/add`, `/new`, or `/edit`
   encodes one team's naming convention as though it were a law of software.
   The moment an application renames a route, URL-based classification
   silently degrades -- exactly the failure mode that matters most for
   software under active development.
3. **Evidence is checkable.** "A password field is present" can be shown to
   a human and agreed with. "The URL contains /auth" cannot be, because it
   is an assumption about someone else's naming.

Each rule below therefore cites what a tester would point at on screen.
Multiple hypotheses may be produced and are RANKED rather than collapsed --
a modal containing a confirmation prompt is legitimately both, and saying so
is more truthful than forcing a single label.

Contrast with `app/agent/explorer.py:classify_deterministic`, which is
URL-and-title driven and is retained unchanged for backward compatibility.
This engine does not replace it; it provides an independent, evidence-based
second opinion that downstream reasoning can prefer. See
`docs/ADAPTIVE_APPLICATION_UNDERSTANDING_ENGINE.md`.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.adaptive_understanding.schemas import ApplicationStateAssessment, StateHypothesis

# Field types that indicate a credential entry surface. `password` is an
# HTML input type -- a web standard, not an application convention.
_CREDENTIAL_FIELD_TYPES = frozenset({"password"})

# HTTP status classes carry standardised meaning (RFC 9110). Using them is
# reading a protocol, not guessing at an application's conventions.
_PERMISSION_DENIED_STATUSES = frozenset({401, 403})
_BACKEND_FAILURE_STATUS_FLOOR = 500


def _hypothesis(state: str, confidence: float, supporting: list[str], contradicting: list[str] | None = None) -> StateHypothesis:
    return StateHypothesis(
        hypothesis_id=f"state-hypothesis:{state}",
        state=state,
        confidence=confidence,
        supporting_evidence=supporting,
        contradicting_evidence=contradicting or [],
    )


def _forms(model: Any) -> list[Any]:
    return list(getattr(model, "forms", None) or [])


def _open_dialogs(model: Any) -> list[Any]:
    return [d for d in (getattr(model, "dialogs", None) or []) if getattr(d, "is_open", True)]


def _alerts_by_severity(model: Any, severity: str) -> list[Any]:
    return [a for a in (getattr(model, "alerts", None) or []) if getattr(a, "severity", "") == severity]


def _network_statuses(model: Any) -> list[int]:
    out = []
    for entry in getattr(model, "network_evidence", None) or []:
        status = getattr(entry, "http_status", None)
        if isinstance(status, int):
            out.append(status)
    return out


def _has_credential_field(model: Any) -> bool:
    for form in _forms(model):
        for field in getattr(form, "fields", None) or []:
            if str(getattr(field, "field_type", "") or "").lower() in _CREDENTIAL_FIELD_TYPES:
                return True
    for el in getattr(model, "interactive_elements", None) or []:
        if str(getattr(el, "input_type", "") or "").lower() in _CREDENTIAL_FIELD_TYPES:
            return True
    return False


def _prepopulated_form(model: Any) -> bool:
    """A form whose fields already carry values is being EDITED; an empty one
    is being CREATED. This is a structural distinction, not a URL one."""
    for form in _forms(model):
        fields = [f for f in (getattr(form, "fields", None) or []) if not getattr(f, "disabled", False)]
        if not fields:
            continue
        filled = sum(1 for f in fields if str(getattr(f, "current_value", "") or "").strip())
        if filled and filled >= max(1, len(fields) // 2):
            return True
    return False


def _collections_with_rows(model: Any) -> tuple[int, int]:
    """(collections present, total rows across them)."""
    colls = getattr(model, "collections", None) or []
    if not colls:
        tables = getattr(model, "tables", None) or []
        rows = sum(len(getattr(t, "rows", None) or []) for t in tables)
        return len(tables), rows
    return len(colls), sum(int(getattr(c, "row_count", 0) or 0) for c in colls)


def _disabled_ratio(model: Any) -> tuple[int, int]:
    els = getattr(model, "interactive_elements", None) or []
    if not els:
        return 0, 0
    disabled = sum(1 for e in els if getattr(e, "disabled", False))
    return disabled, len(els)


def classify(model: Any, *, readiness_score: float = 1.0, page_state: Any = None) -> ApplicationStateAssessment:
    """Produce ranked state hypotheses from structural evidence.

    `readiness_score` is passed in so that an unrendered screen is reported
    as `loading`/`initializing` rather than being mislabelled from the
    fragments that happen to exist -- classifying an empty document as an
    "empty state" would be a serious and misleading error.
    """
    hypotheses: list[StateHypothesis] = []

    interactive = list(getattr(model, "interactive_elements", None) or [])
    headings = list(getattr(model, "headings", None) or [])
    text_blocks = list(getattr(model, "text_blocks", None) or [])
    forms = _forms(model)
    open_dialogs = _open_dialogs(model)
    coll_count, row_total = _collections_with_rows(model)
    statuses = _network_statuses(model)

    # -- Not-yet-rendered states come first: nothing else can be trusted -----
    if not interactive and not headings and not text_blocks:
        hypotheses.append(
            _hypothesis(
                "initializing",
                0.85,
                ["no controls, headings, or text have rendered -- the document shell exists but its content does not"],
            )
        )
    elif readiness_score < 0.45:
        hypotheses.append(
            _hypothesis(
                "loading",
                0.7,
                [f"readiness evidence is weak ({readiness_score:.2f}); the screen appears to still be assembling itself"],
            )
        )

    # -- Protocol-level failure states (RFC 9110 status semantics) ----------
    failing = [s for s in statuses if s >= _BACKEND_FAILURE_STATUS_FLOOR]
    if failing:
        hypotheses.append(
            _hypothesis(
                "backend_failure",
                0.9,
                [f"the server returned {', '.join(str(s) for s in sorted(set(failing)))} for at least one request on this screen"],
            )
        )

    denied = [s for s in statuses if s in _PERMISSION_DENIED_STATUSES]
    if denied:
        hypotheses.append(
            _hypothesis(
                "permission_denied",
                0.85,
                [f"the server returned {', '.join(str(s) for s in sorted(set(denied)))}, which is the standard way to refuse access"],
            )
        )

    if getattr(page_state, "console_errors", None):
        count = len(page_state.console_errors)
        hypotheses.append(
            _hypothesis(
                "frontend_failure",
                0.5 if count < 3 else 0.7,
                [f"{count} scripting error(s) occurred while rendering this screen"],
                ["scripting errors do not always prevent a screen from being usable"],
            )
        )

    # -- Credential entry: an HTML-standard password input, never a URL -----
    if _has_credential_field(model):
        hypotheses.append(
            _hypothesis(
                "authentication",
                0.92,
                ["a password input is present, which only appears where credentials are collected"],
            )
        )

    # -- Overlay states ------------------------------------------------------
    if open_dialogs:
        dialog_labels = [str(getattr(d, "accessible_name", "") or getattr(d, "stable_id", "") or "dialog") for d in open_dialogs]
        hypotheses.append(
            _hypothesis(
                "modal",
                0.8,
                [f"{len(open_dialogs)} dialog/modal is open, which takes precedence over whatever is behind it: {', '.join(dialog_labels[:3])}"],
            )
        )
        # A confirmation is a dialog whose controls are a binary decision
        # over an action already initiated -- detected by SHAPE (few controls,
        # no data entry), not by matching prompt wording in any language.
        for dialog in open_dialogs:
            contained = list(getattr(dialog, "contained_element_ids", None) or [])
            dialog_forms = [f for f in forms if getattr(f, "form_id", None) in contained]
            if contained and not dialog_forms and 1 <= len(contained) <= 4:
                hypotheses.append(
                    _hypothesis(
                        "confirmation",
                        0.65,
                        ["an open dialog offers only a small set of controls and collects no input -- the shape of a decision prompt"],
                        ["a small dialog may also be an informational notice rather than a decision point"],
                    )
                )
                break

    # -- Validation failure: error-severity alerts on a screen with a form --
    error_alerts = _alerts_by_severity(model, "error")
    if error_alerts and forms:
        hypotheses.append(
            _hypothesis(
                "validation_failure",
                0.75,
                [f"{len(error_alerts)} error-severity alert(s) are shown on a screen that contains a form -- the shape of rejected input"],
                ["an error alert on a form screen may also report a failure unrelated to the input"],
            )
        )

    # -- Data-bearing states -------------------------------------------------
    if coll_count and row_total > 0:
        hypotheses.append(
            _hypothesis(
                "collection",
                0.8,
                [f"{coll_count} record collection(s) with {row_total} row(s) are displayed"],
            )
        )
    elif coll_count and row_total == 0:
        hypotheses.append(
            _hypothesis(
                "empty_state",
                0.75,
                [f"{coll_count} record collection(s) are present but contain no rows -- the surface exists, the data does not"],
            )
        )
        hypotheses.append(
            _hypothesis(
                "no_data",
                0.5,
                ["a collection rendered with zero rows"],
                ["this may be a legitimately empty dataset rather than a defect or an unfinished feature"],
            )
        )

    # -- Form-bearing states -------------------------------------------------
    if forms and not _has_credential_field(model):
        if _prepopulated_form(model):
            hypotheses.append(
                _hypothesis(
                    "edit",
                    0.7,
                    ["a form is present whose fields already carry values -- an existing record is being modified"],
                )
            )
        else:
            hypotheses.append(
                _hypothesis(
                    "create",
                    0.6,
                    ["a form is present with empty fields -- a new record is being entered"],
                    ["an empty form may also be a search or filter surface"],
                )
            )

    # -- Wholly-disabled surface: a feature that exists but cannot be used ---
    disabled, total_controls = _disabled_ratio(model)
    if total_controls >= 3 and disabled == total_controls:
        hypotheses.append(
            _hypothesis(
                "feature_disabled",
                0.6,
                [f"all {total_controls} controls on this screen are disabled -- the surface is present but unusable"],
            )
        )

    # -- Generic interactive fallbacks --------------------------------------
    if interactive and readiness_score >= 0.6:
        confidence = 0.4 if len(interactive) >= 3 else 0.3
        hypotheses.append(
            _hypothesis(
                "interactive",
                confidence,
                [f"{len(interactive)} usable control(s) are present and readiness evidence is adequate"],
            )
        )
    elif interactive:
        hypotheses.append(
            _hypothesis(
                "partially_interactive",
                0.45,
                [f"{len(interactive)} control(s) are present but readiness evidence is weak ({readiness_score:.2f})"],
            )
        )

    if not hypotheses:
        return ApplicationStateAssessment(
            assessment_id="state:unknown",
            primary_state="unknown",
            confidence=0.0,
            hypotheses=[],
            unknown_reason=(
                "No structural evidence matched any known application state. This is a finding worth "
                "investigating, not a failure -- the screen may use a presentation this engine has not "
                "yet learned to recognise."
            ),
        )

    # Deterministic ranking: confidence first, then state name so equal-
    # confidence hypotheses never reorder between runs.
    hypotheses.sort(key=lambda h: (-h.confidence, h.state))
    primary = hypotheses[0]

    return ApplicationStateAssessment(
        assessment_id=f"state:{primary.state}",
        primary_state=primary.state,
        confidence=primary.confidence,
        hypotheses=hypotheses,
    )

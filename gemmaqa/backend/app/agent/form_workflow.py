"""Generic safe form workflow — generalizes the mature AuthWorkflow state-machine
pattern (app.agent.auth_strategy) to ANY safe data-entry form: "create test
customer/contact/employee", checkout information, add-product-to-cart-adjacent
forms, and similar. AuthenticationStrategy's own login/registration machinery is
untouched — this is a parallel, independent engine for non-auth forms, not a
refactor of it, so authentication behaviour carries zero risk from this change.

Emits ordinary BrowserActions (FILL/SELECT/CHECK/CLICK) through the normal
Planner -> ActionValidator -> ActionExecutor -> BrowserAdapter pipeline — this
workflow never touches Playwright directly and never invents a selector.

Must NEVER plan a delete / real-payment / account-closure / irreversible-production
step. Safety is enforced at TWO layers: (1) the gate deciding which forms are even
eligible to start a workflow (safe_test_data_create's existing keyword exclusions
plus the DISALLOWED_SUBMIT_HINTS check on the submit control below), and (2) the
ActionValidator, which independently re-checks every action regardless of what this
workflow requests.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.field_constraint_inference import infer_constraint
from app.agent.form_identity import form_signature
from app.agent.test_data import values_for_field
from app.agent.submission_outcome import SUBMISSION_ACCEPTED, SUBMISSION_UNKNOWN
from app.safety.policies import TEST_DATA_PREFIX
from app.agent.test_data_generator import generate_conservative_value, generate_valid_value
from app.schemas import ActionCategory, ActionType, BrowserAction, FormDescriptor, RiskLevel
from app.utils.ids import new_id
from app.utils.logging import get_logger

# Semantic types most likely to identify the created record afterward
# (email/username are almost always unique; a name alone usually isn't) —
# used by `plan_data` to pick `primary_identity_value` for later
# verification/cleanup lookup. Order = priority.
logger = get_logger("agent.form_workflow")

_IDENTITY_PRIORITY = ("email", "username", "employee_identifier", "url", "free_text")

# Field types that name a record of ANOTHER entity rather than carrying a value
# of their own. When one of these offers nothing to pick, the record it points
# at does not exist yet, and inventing a value hides that.
_REFERENCE_SEMANTIC_TYPES = frozenset({"autocomplete_selection", "dropdown_option"})

# Semantic types safe to CHANGE on an existing record. Deliberately excludes
# email/username/identifiers (often unique-constrained, so changing one tests
# uniqueness rather than updating) and every format-constrained type (a failed
# format is not an update failure). Names and free text are the honest choice.
_UPDATE_SAFE_SEMANTIC_TYPES = frozenset(
    {"first_name", "middle_name", "last_name", "free_text", "description", "city", "state_province", "country"}
)

# Shape heuristics for "which value did the application probably object to?"
# See GenericFormWorkflow._unattributed_retry_targets. Length 30 sits below the
# most common short-text column caps (32/40/50) without flagging ordinary names
# or emails; the character class allows what plain text inputs normally carry.
_RISKY_VALUE_LENGTH = 30
_UNUSUAL_CHARACTERS = re.compile(r"[^A-Za-z0-9 @._-]")

FORM_WORKFLOW_STATES = frozenset(
    {
        "discovered",
        "inspected",
        "classified",
        "data_planned",
        "filling",
        "ready_to_submit",
        "submitted",
        "verified",
        "failed",
        "blocked",
        "skipped",
    }
)

FORM_PURPOSES = frozenset(
    {
        "safe_test_data_creation",
        "checkout_information",
        "search_or_filter",
        "unknown",
    }
)

# A submit control matching any of these must never be triggered by this workflow,
# regardless of what form-purpose classification concluded — this is the hard floor,
# independent of (and in addition to) the ActionValidator's own checks.
# How many individual fields may fail to fill before the form is abandoned
# rather than submitted incomplete. One is a quirk (a hidden upload control, a
# widget that needs a different verb); several means the form is not
# operable by this run.
MAX_FAILED_FIELDS = 3

DISALLOWED_SUBMIT_HINTS = (
    "delete",
    "remove account",
    "close account",
    "deactivate",
    "cancel subscription",
    "wire transfer",
    "confirm payment",
)

CHECKOUT_HINTS = ("checkout", "billing", "shipping", "payment info", "order")

# A recorded submit_element_id whose label matches one of these is almost certainly
# NOT the real submit control (a form commonly has both "Cancel" and "Continue"/
# "Submit" buttons; the browser layer's own submit-detection heuristic can pick the
# wrong one). This workflow must never rely on it blindly — see _resolve_submit_id.
NON_PROGRESS_SUBMIT_HINTS = ("cancel", "back", "close", "dismiss")
PROGRESS_HINTS = (
    "continue",
    "submit",
    "save",
    "finish",
    "next",
    "proceed",
    "place order",
    "confirm",
    "create",
)


def _element_label(el: Any) -> str:
    return str(
        getattr(el, "accessible_name", None)
        or getattr(el, "visible_text", None)
        or getattr(el, "text", None)
        or getattr(el, "current_value", None)
        or ""
    ).strip()


def _resolve_submit_id(
    form: FormDescriptor, *, interactive_elements: list[Any] | None
) -> tuple[str | None, str]:
    """Return (element_id, label) for the control that should actually be clicked to
    submit/advance this form. Falls back to searching all interactive elements for a
    better candidate when the recorded submit_element_id's label looks like a
    non-progressing control (e.g. "Cancel") rather than trusting it blindly."""
    submit_id = form.submit_element_id
    label = ""
    if submit_id:
        submit_field = next((f for f in form.fields if f.element_id == submit_id), None)
        if submit_field is not None:
            label = submit_field.label or ""
        if not label:
            el = next(
                (e for e in (interactive_elements or []) if getattr(e, "element_id", None) == submit_id),
                None,
            )
            if el is not None:
                label = _element_label(el)

    if submit_id and not any(h in label.lower() for h in NON_PROGRESS_SUBMIT_HINTS):
        return submit_id, label

    # Recorded submit control is missing or looks like "Cancel"/"Back" — search for a
    # real <input type="submit"> or a button whose text is progress-oriented instead.
    field_ids = {f.element_id for f in form.fields if f.element_id}
    best: tuple[str, str] | None = None
    for el in interactive_elements or []:
        el_id = getattr(el, "element_id", None)
        if not el_id or el_id in field_ids or el_id == submit_id:
            continue
        el_label = _element_label(el)
        input_type = (getattr(el, "input_type", None) or "").lower()
        tag = (getattr(el, "tag", None) or "").lower()
        if input_type == "submit":
            return el_id, el_label or "Submit"
        if any(h in el_label.lower() for h in PROGRESS_HINTS) and tag in {"button", "input", "a"}:
            best = best or (el_id, el_label)
    if best is not None:
        return best
    return submit_id, label


def infer_form_purpose(form: FormDescriptor, *, page_url: str = "", heading: str = "") -> str:
    """Deterministic, non-app-specific form-purpose classification from field labels
    and page context — never a hardcoded per-target-app name."""
    blob = " ".join(
        [
            page_url.lower(),
            heading.lower(),
            " ".join((f.label or f.name or "") for f in form.fields).lower(),
        ]
    )
    if any(h in blob for h in CHECKOUT_HINTS):
        return "checkout_information"
    if any(h in blob for h in ("search", "filter", "query")) and len(form.fields) <= 2:
        return "search_or_filter"
    if _arrives_prefilled(form):
        # A form that arrives already carrying the record's values is an UPDATE,
        # not a create — the record it edits already exists. Structural, so it
        # holds without any per-application knowledge, and it matters: treating
        # an edit form as a create meant overwriting every field (one action
        # each) instead of changing one and verifying that one changed.
        return "safe_test_data_update"
    if form.fields:
        return "safe_test_data_creation"
    return "unknown"


# A form is treated as prefilled when this fraction of its fillable fields
# already carry a value. Not "any field", because a single defaulted dropdown or
# a remembered country does not make a create form an edit form.
_PREFILLED_FIELD_FRACTION = 0.5


def _arrives_prefilled(form: FormDescriptor) -> bool:
    fillable = [
        f
        for f in (form.fields or [])
        if (getattr(f, "field_type", "") or "text").lower() != "password"
        and not getattr(f, "disabled", False)
    ]
    if len(fillable) < 2:
        return False
    filled = sum(1 for f in fillable if (getattr(f, "current_value", None) or "").strip())
    return filled >= max(2, int(len(fillable) * _PREFILLED_FIELD_FRACTION))


def _is_choice_field(field_type: str) -> bool:
    return field_type in {"select", "dropdown", "radio"}


def _is_toggle_field(field_type: str) -> bool:
    return field_type in {"checkbox"}


@dataclass
class GenericFormWorkflow:
    """A live, in-progress safe-form-fill run for one form. Mirrors AuthWorkflow's
    step-by-step next_action()/advance() shape so it plugs into the same frontier
    candidate_type + goal_type dispatch path (continue_form_workflow /
    continue_workflow) without inventing a second execution pipeline."""

    workflow_id: str
    form_id: str
    page_url: str
    purpose: str
    fields: list[Any] = field(default_factory=list)
    submit_element_id: str | None = None
    run_id: str = ""

    state: str = "discovered"
    planned_values: dict[str, str] = field(default_factory=dict)
    # The single value most likely to identify the record this workflow
    # creates, once submitted — set by `plan_data()`, consumed by
    # `app.agent.creation_verifier`/`app.agent.temporary_record_registry`
    # via the controller's post-submit hook. None until data is planned.
    primary_identity_value: str | None = None
    primary_identity_semantic_type: str | None = None
    filled_element_ids: set[str] = field(default_factory=set)
    attempts: int = 0
    max_attempts: int = 3
    # Bounded regardless of WHY a step might not converge (e.g. an observed page
    # re-rendering with fresh element ids between steps, so filled_element_ids never
    # matches what's on the current page) — every next_action() call counts, not just
    # failed submits, so this workflow can never loop beyond its own field count plus
    # a little slack for retries.
    total_steps: int = 0
    last_error: str | None = None
    # Every refusal this workflow has seen, as returned by
    # `submission_outcome.SubmissionOutcome.to_dict()`. Reported rather than
    # discarded: "the application refused three different value sets" is a
    # finding, not noise.
    rejection_history: list[dict[str, Any]] = field(default_factory=list)
    # Labels of required lookups that reference a record which does not exist —
    # the form cannot be completed until that record is created elsewhere. Filled
    # by `plan_data` instead of inventing a value; see _REFERENCE_SEMANTIC_TYPES.
    unsatisfied_references: list[str] = field(default_factory=list)
    # Why this form was abandoned, in words an operator can act on. Reaches
    # the activity log and the final report; `state == "blocked"` alone says
    # nothing about what to do next.
    blocked_reason: str = ""
    _last_rejection_fingerprint: str = ""
    # Fields whose fill failed. Recorded rather than fatal: one unfillable
    # field must not cost the form every field after it (see
    # `note_field_failed`).
    failed_field_ids: list[str] = field(default_factory=list)
    # Stable identity of the form this workflow operates, from
    # `app.agent.form_identity`. `form_id` cannot serve: it is a document-order
    # counter that a successful submit frequently changes.
    form_signature: str = ""
    # Set only for `safe_test_data_update` workflows: which single field this
    # workflow changed, and its before/after values. Consumed by the
    # controller's post-submit hook to advance the Temporary Record Registry
    # from `verified` to `updated` with a real before/after pair.
    updated_field_element_id: str | None = None
    updated_field_before_value: str | None = None
    updated_field_after_value: str | None = None
    # Fields already downgraded to their conservative value, so a second
    # refusal does not pointlessly "retry" them with the identical fallback.
    _conservative_element_ids: set[str] = field(default_factory=set)
    # This workflow itself never attempts deletion — created values are
    # clearly tagged as test data (via app.safety.policies.TEST_DATA_PREFIX
    # plus a run/timestamp fragment, see app.agent.test_data_generator) and,
    # once verified, registered in app.agent.temporary_record_registry for
    # the SEPARATE cleanup-planning step to act on.
    rollback_hint: str = "Created values are tagged as GemmaQA test data; see the Temporary Record Registry for cleanup status."

    @classmethod
    def start(
        cls,
        form: FormDescriptor,
        *,
        page_url: str,
        heading: str = "",
        interactive_elements: list[Any] | None = None,
        run_id: str = "",
    ) -> "GenericFormWorkflow | None":
        purpose = infer_form_purpose(form, page_url=page_url, heading=heading)
        if purpose == "unknown":
            return None
        submit_id, label = _resolve_submit_id(form, interactive_elements=interactive_elements)
        if submit_id:
            if any(h in label.lower() for h in DISALLOWED_SUBMIT_HINTS):
                return None
        wf = cls(
            workflow_id=new_id(),
            form_id=form.form_id,
            page_url=page_url,
            purpose=purpose,
            fields=list(form.fields),
            submit_element_id=submit_id,
            state="inspected",
            run_id=run_id,
        )
        # `form_id` is positional and changes when the page re-renders — which a
        # successful submit usually causes. Everything that must remember what
        # this run has already done to this form keys on the signature instead.
        wf.form_signature = form_signature(form, page_url)
        wf.state = "classified"
        return wf

    def plan_data(self) -> None:
        """classified -> data_planned: pick one safe, positive value per
        fillable field via semantic-type + constraint inference (see
        app.agent.field_constraint_inference/test_data_generator) — never a
        negative/invalid probe (this is a completion workflow, not a
        negative-test scenario). Falls back to the older, simpler
        `values_for_field` catalogue only if semantic-type generation
        produces nothing usable, so a constraint-inference edge case can
        never leave a field silently unfilled."""
        if self.purpose == "safe_test_data_update":
            self._plan_single_field_update()
            return

        identity_candidates: dict[str, str] = {}
        for f in self.fields:
            el_id = getattr(f, "element_id", None)
            field_type = (getattr(f, "field_type", "") or "text").lower()
            if not el_id or field_type == "password" or getattr(f, "disabled", False):
                continue
            if el_id in self.planned_values:
                continue
            if _is_choice_field(field_type):
                options = [o for o in (getattr(f, "options", None) or []) if o]
                if options:
                    self.planned_values[el_id] = options[0]
                continue
            if _is_toggle_field(field_type):
                self.planned_values[el_id] = "true"
                continue

            constraint = infer_constraint(f)

            # A lookup offering nothing to pick REFERENCES a record that does not
            # exist. It is not a blank to fill.
            #
            # OrangeHRM's Admin -> Add User requires "Employee Name", an
            # autocomplete accepting only employees created in PIM. Because an
            # autocomplete is a TEXT input it fell past the choice-field branch
            # above — which correctly plans nothing when there are no options —
            # and reached the generator, which fabricated
            # `GemmaQA_TEST_<run>_option`. The form could never submit, and the
            # real finding ("this form needs an employee and there isn't one")
            # stayed hidden behind an invalid value resubmitted 27 times a run.
            #
            # This branch catches only lookups the markup DECLARES (role,
            # aria-autocomplete, a bound option list). OrangeHRM's declares
            # nothing — probed live, that input has no role, no
            # aria-autocomplete, no list and no name, only a placeholder — so
            # the same question is asked again at fill time, by behaviour, in
            # `app.browser.custom_controls.resolve_lookup`. Two detectors for one
            # rule is deliberate here and the reason is worth stating: they read
            # different evidence, and the DOM one cannot see what the page only
            # reveals when typed into.
            if constraint.field_semantic_type in _REFERENCE_SEMANTIC_TYPES and not (
                getattr(f, "options", None) or []
            ):
                label = (getattr(f, "label", None) or el_id or "").strip()
                if label and label not in self.unsatisfied_references:
                    self.unsatisfied_references.append(label)
                logger.info(
                    "Form %s field %r references a record that does not exist; leaving it "
                    "empty rather than inventing one", self.form_id, label or el_id,
                )
                continue

            value = None
            try:
                test_value = generate_valid_value(constraint.field_semantic_type, constraint, run_id=self.run_id)
                value = test_value.generated_value
            except Exception:
                value = None
            if not value:
                candidates = values_for_field(field_type, getattr(f, "label", None))
                positive = next(
                    (v for v in candidates if v.category != "empty" and "invalid" not in v.category),
                    None,
                )
                value = positive.value if positive else None
            if value:
                self.planned_values[el_id] = value
                if constraint.field_semantic_type in _IDENTITY_PRIORITY:
                    identity_candidates[constraint.field_semantic_type] = value

        for semantic_type in _IDENTITY_PRIORITY:
            if semantic_type in identity_candidates:
                self.primary_identity_value = identity_candidates[semantic_type]
                self.primary_identity_semantic_type = semantic_type
                break
        if self.primary_identity_value is None and self.planned_values:
            self.primary_identity_value = next(iter(self.planned_values.values()))

        self.state = "data_planned"

    def _plan_single_field_update(self) -> None:
        """classified -> data_planned for an UPDATE: change exactly one field.

        An update test asks "does changing this one thing persist?", so
        overwriting every field answers a different (and weaker) question while
        costing one action per field — on an eleven-field form that is the
        difference between 2 actions and 12, and on a wide form it was the
        difference between finishing the CRUD cycle and running out of budget.

        The field chosen is a plain free-text one that already has a value:
        changing something already populated makes before/after comparison
        meaningful, and avoiding typed/constrained fields keeps the edit from
        failing validation for reasons unrelated to updating.
        """
        chosen = None
        for f in self.fields:
            el_id = getattr(f, "element_id", None)
            field_type = (getattr(f, "field_type", "") or "text").lower()
            if not el_id or field_type == "password" or getattr(f, "disabled", False):
                continue
            if _is_choice_field(field_type) or _is_toggle_field(field_type):
                continue
            if not (getattr(f, "current_value", None) or "").strip():
                continue
            constraint = infer_constraint(f)
            if constraint.field_semantic_type not in _UPDATE_SAFE_SEMANTIC_TYPES:
                continue
            chosen = (f, el_id, constraint)
            break

        if chosen is None:
            # Nothing safely changeable — say so rather than mutating a field
            # whose format we would probably break.
            self.state = "blocked"
            self.last_error = "no_safely_updatable_field"
            return

        f, el_id, constraint = chosen
        self.updated_field_before_value = (getattr(f, "current_value", None) or "").strip()
        try:
            value = generate_conservative_value(
                constraint.field_semantic_type, constraint, run_id=self.run_id
            ).generated_value
        except Exception:  # pragma: no cover - defensive
            value = None
        if not value or value == self.updated_field_before_value:
            value = f"{self.updated_field_before_value} Updated"[:60]

        self.planned_values[el_id] = value
        self.updated_field_element_id = el_id
        self.updated_field_after_value = value
        self.state = "data_planned"

    def next_action(self) -> BrowserAction | None:
        """Advance the state machine by exactly one step and return the BrowserAction
        for it, or None if this workflow has nothing further to do right now."""
        self.total_steps += 1
        # Each attempt gets its own pass over the form: one step per field, one
        # to submit, plus slack. Growing with `attempts` (not a flat allowance)
        # is what lets a rejected submit be corrected and re-submitted — the
        # flat version budgeted a single extra step per attempt, so a retry ran
        # out of steps mid-refill and the workflow died one action before it
        # would have re-submitted. `attempts` is itself capped by
        # `max_attempts`, so this stays bounded.
        step_budget = (len(self.fields) + 2) * (1 + self.attempts)
        if self.total_steps > step_budget:
            self.state = "failed"
            self.last_error = "step_budget_exceeded"
            return None
        if self.state == "classified":
            self.plan_data()
        if self.state == "data_planned":
            self.state = "filling"
        if self.state == "filling":
            for f in self.fields:
                el_id = getattr(f, "element_id", None)
                if not el_id or el_id in self.filled_element_ids:
                    continue
                value = self.planned_values.get(el_id)
                if value is None:
                    continue
                field_type = (getattr(f, "field_type", "") or "text").lower()
                self.filled_element_ids.add(el_id)
                if _is_choice_field(field_type):
                    action_type = ActionType.SELECT
                elif _is_toggle_field(field_type):
                    action_type = ActionType.CHECK
                else:
                    action_type = ActionType.FILL
                return BrowserAction(
                    action=action_type,
                    element_id=el_id,
                    value=value,
                    reason=f"Fill safe test data for '{getattr(f, 'label', None) or el_id}'",
                    expected_result="Field accepts the value; no submission yet.",
                    risk=RiskLevel.LOW,
                    category=ActionCategory.EXPLORATION,
                    metadata={
                        "workflow_id": self.workflow_id,
                        "form_id": self.form_id,
                        "form_workflow_write": True,
                        "value_category": "form_workflow_fill",
                        # Already produced by the test-data generator, which
                        # applies the traceability marker wherever the field's
                        # type can carry one. The safety validator must not
                        # prefix it a second time — that is what corrupted
                        # dates, phone numbers, and length-capped fields.
                        "generated_test_data": True,
                    },
                )
            self.state = "ready_to_submit"
        if self.state == "ready_to_submit":
            if not self.submit_element_id:
                self.state = "failed"
                self.last_error = "no_submit_control_found"
                return None
            self.state = "submitted"
            return BrowserAction(
                action=ActionType.CLICK,
                element_id=self.submit_element_id,
                reason="Submit safe test data / continue form",
                expected_result="Form submits, or the multi-step flow advances.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EXPLORATION,
                metadata={
                    "workflow_id": self.workflow_id,
                    "form_id": self.form_id,
                    "form_workflow_write": True,
                    "form_workflow_submit": True,
                },
            )
        return None

    def advance_multi_step(self, form: FormDescriptor, *, page_url: str, heading: str = "") -> None:
        """A multi-step flow (e.g. checkout info -> overview -> confirmation) may
        present a NEW form on the next page after a submit. Re-target this SAME
        workflow at it instead of starting an unrelated second workflow, so the whole
        flow is tracked as one continue_workflow goal end-to-end."""
        self.form_id = form.form_id
        self.page_url = page_url
        self.fields = list(form.fields)
        self.submit_element_id = form.submit_element_id
        self.filled_element_ids = set()
        self.planned_values = {}
        self.state = "classified"
        self.purpose = infer_form_purpose(form, page_url=page_url, heading=heading) or self.purpose

    def note_unsatisfied_reference(self, element_id: str, detail: str = "") -> None:
        """A field turned out to name a record the application does not hold.

        Detected at fill time, not plan time: a lookup is indistinguishable from
        a text box until something is typed into it (see
        `app.browser.custom_controls`). Blocking here rather than counting an
        attempt is deliberate — no amount of retrying creates the missing
        record, so a retry budget would only spend the run's actions restating
        the same impossibility.
        """
        label = ""
        for f in self.fields:
            if getattr(f, "element_id", None) == element_id:
                label = (getattr(f, "label", None) or "").strip()
                break
        label = label or element_id
        if label not in self.unsatisfied_references:
            self.unsatisfied_references.append(label)
        self.state = "blocked"
        self.last_error = "unsatisfied_record_reference"
        self.blocked_reason = (
            f"'{label}' only accepts records the application already holds, and it "
            "offered none. This form cannot be completed until such a record exists"
            + (f" ({detail})" if detail else "")
        )
        logger.info("Form %s blocked on an unsatisfiable reference: %s",
                    self.form_id, self.blocked_reason)

    def note_field_failed(self, element_id: str) -> None:
        """ONE field could not be filled. Keep filling the rest.

        This used to route through `note_result(success=False)`, which is the
        SUBMIT path: it sets `state = "ready_to_submit"`. So a single failed
        field jumped the whole state machine past every remaining field and
        submitted the form half-empty.

        Observed live on OrangeHRM's Add Candidate: the Resume upload failed,
        and Keywords, Notes and the consent checkbox were never filled — the
        form was submitted without them, and the resulting non-response was
        recorded as an application bug. GemmaQA blamed the application for a
        failure it caused itself.

        The field is already in `filled_element_ids`, so it is not retried; the
        loop simply moves to the next one. Bounded by `MAX_FAILED_FIELDS` so a
        form where nothing can be filled still gives up rather than grinding
        through its whole step budget.
        """
        if element_id and element_id not in self.failed_field_ids:
            self.failed_field_ids.append(element_id)
        self.last_error = f"field_fill_failed:{element_id}"
        if len(self.failed_field_ids) >= MAX_FAILED_FIELDS:
            self.state = "failed"
            self.blocked_reason = (
                f"{len(self.failed_field_ids)} fields could not be filled "
                f"({', '.join(self.failed_field_ids)}); the form was abandoned "
                "rather than submitted incomplete"
            )
            logger.info("Form %s failed: %s", self.form_id, self.blocked_reason)
            return
        logger.info(
            "Form %s could not fill %s; continuing with the remaining fields",
            self.form_id, element_id,
        )

    def note_result(self, *, success: bool, outcome: Any = None) -> None:
        """Record what happened to a submit.

        `success` is whether the CLICK executed. `outcome` (a
        `app.agent.submission_outcome.SubmissionOutcome`) is whether the
        APPLICATION accepted the data — a different question, and the one that
        determines whether this workflow actually achieved anything.

        Live-run finding: this method used to treat a successful click as a
        verified creation. A form whose submit returned `400 Bad Request` was
        recorded as a completed create, and the run then re-opened the same form
        to submit the same rejected values again. `outcome` is what closes that.
        When it is absent the old click-based behaviour is kept, so existing
        callers are unaffected.
        """
        if outcome is None:
            if success:
                self.state = "verified"
                return
            self.attempts += 1
            self.last_error = "submit_failed"
            self.state = "failed" if self.attempts >= self.max_attempts else "ready_to_submit"
            return

        verdict = getattr(outcome, "outcome", None)
        if success and verdict == SUBMISSION_ACCEPTED:
            self.state = "verified"
            self.last_error = None
            return

        if success and verdict == SUBMISSION_UNKNOWN:
            # Nothing proved acceptance and nothing proved refusal. Treat it as
            # done rather than burning the budget re-submitting into silence,
            # but say so: an unverified submit is not a verified one.
            self.state = "submitted_unverified"
            self.last_error = "submission_outcome_unknown"
            return

        self.attempts += 1
        self.last_error = f"submission_{verdict or 'rejected'}"
        self.rejection_history.append(getattr(outcome, "to_dict", lambda: {})())

        # The same data refused in the same way is not new information. Retrying
        # it cannot succeed, and a run that keeps trying spends its whole budget
        # learning nothing: observed live at 27 identical submits, ~60% of the
        # action budget, on a form whose required fields could never be filled.
        #
        # `max_attempts` alone does not catch this — it counts attempts within
        # ONE workflow, and it is 3. Blocking on an unchanged rejection stops the
        # cycle at the first repeat regardless of how the attempt count is
        # reached.
        fingerprint = self._rejection_fingerprint(outcome)
        if fingerprint and fingerprint == self._last_rejection_fingerprint:
            self.state = "blocked"
            self.last_error = "identical_rejection_with_unchanged_data"
            self.blocked_reason = (
                "The application refused the same data in the same way twice. "
                "Retrying cannot change the outcome, so this form was abandoned: "
                f"{self._describe_rejection(outcome)}"
            )
            logger.info(
                "Form %s blocked after an identical rejection: %s",
                self.form_id, self.blocked_reason,
            )
            return
        self._last_rejection_fingerprint = fingerprint

        if self.attempts >= self.max_attempts:
            self.state = "failed"
            return

        # The application refused this data. Repeating it verbatim would refuse
        # again, so re-plan the implicated fields conservatively and refill.
        # When the refusal could not be attributed to a specific field (usual
        # case — browsers do not expose response bodies), every re-plannable
        # field is retried conservatively rather than none.
        targets = list(getattr(outcome, "rejected_element_ids", None) or [])
        if not targets:
            targets = self._unattributed_retry_targets()
        if not targets:
            self.state = "failed"
            self.last_error = "no_remaining_retry_strategy"
            return

        self.replan_conservatively(targets)
        self.state = "filling"


    def _rejection_fingerprint(self, outcome: Any) -> str:
        """What this refusal WAS, together with the data that caused it.

        Two refusals match only when the application said the same thing AND the
        values sent were the same. A retry that genuinely changed a field
        produces a different fingerprint and is allowed to proceed — the point is
        to stop repetition, not to stop retrying.
        """
        verdict = str(getattr(outcome, "outcome", "") or "")
        status = str(getattr(outcome, "http_status", "") or "")
        messages = "|".join(sorted(str(m) for m in (getattr(outcome, "messages", None) or [])))
        data = "|".join(f"{k}={v}" for k, v in sorted(self.planned_values.items()))
        return hashlib.sha1(f"{verdict}#{status}#{messages}#{data}".encode("utf-8")).hexdigest()

    @staticmethod
    def _describe_rejection(outcome: Any) -> str:
        messages = [str(m) for m in (getattr(outcome, "messages", None) or []) if m]
        if messages:
            return messages[0][:200]
        status = getattr(outcome, "http_status", None)
        return f"HTTP {status}" if status else str(getattr(outcome, "outcome", "refused"))

    def _unattributed_retry_targets(self) -> list[str]:
        """Which fields to retry when the application refused without saying why.

        Re-typing every field costs one action each, and a form with a dozen
        fields can consume an entire action budget on a single retry — observed
        live, where a blind 11-field refill left nothing for read, update, or
        delete. So target the values a validator is most likely to have objected
        to: the ones that look least like something a person would type.

        The heuristic is shape-based and application-neutral — long values,
        values still carrying the run-scoped test prefix, and values containing
        characters outside the plain set most inputs accept. If nothing looks
        risky, fall back to retrying everything rather than giving up.
        """
        risky: list[str] = []
        remaining: list[str] = []
        for el_id, value in self.planned_values.items():
            if el_id in self._conservative_element_ids:
                continue
            remaining.append(el_id)
            text = str(value)
            if (
                TEST_DATA_PREFIX in text
                or len(text) > _RISKY_VALUE_LENGTH
                or _UNUSUAL_CHARACTERS.search(text)
            ):
                risky.append(el_id)
        return risky or remaining

    def replan_conservatively(self, element_ids: list[str]) -> None:
        """Replace the named fields' values with their most conservative form."""
        by_element = {getattr(f, "element_id", None): f for f in self.fields}
        for el_id in element_ids:
            f = by_element.get(el_id)
            if f is None or el_id not in self.planned_values:
                continue
            constraint = infer_constraint(f)
            try:
                replacement = generate_conservative_value(
                    constraint.field_semantic_type, constraint, run_id=self.run_id
                ).generated_value
            except Exception:  # pragma: no cover - defensive
                continue
            if not replacement or replacement == self.planned_values.get(el_id):
                continue
            self.planned_values[el_id] = replacement
            self._conservative_element_ids.add(el_id)
            # Force a refill of exactly these fields; everything else keeps the
            # value already typed into the live page.
            self.filled_element_ids.discard(el_id)

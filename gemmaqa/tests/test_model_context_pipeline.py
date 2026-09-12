"""Model-context pipeline: aggregation -> projection -> prompt -> provider -> validation.

Group A exists specifically to close the gap recorded in docs/MODEL_CONTEXT_AUDIT.md
question 20: under the default `mock` provider the action path bypasses
`build_action_prompt`, `sanitize_page_state_for_model`, prompt-size handling, and
`parse_and_validate_action`, so a regression in any of them is invisible to CI.
Every test here is network-free.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.gemma.base import ActionGenerationRequest, GemmaProvider  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.schemas import (  # noqa: E402
    ActionType,
    FormDescriptor,
    FormField,
    InteractiveElement,
    PageState,
)
from app.utils.ids import new_id  # noqa: E402
from app.utils.sanitization import MASK  # noqa: E402


# ---------------------------------------------------------------------------
# Deterministic, network-free providers that record exactly what they receive
# ---------------------------------------------------------------------------


class _RecordingMixin:
    """Captures every (system, user, images, temperature) tuple a provider is asked
    to generate from, so tests can assert on the real prompt text."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.seen: list[dict] = []

    async def _generate(self, system, user, *, images=None, temperature=None):  # type: ignore[no-untyped-def]
        self.seen.append(
            {"system": system, "user": user, "images": list(images or []), "temperature": temperature}
        )
        return await super()._generate(system, user, images=images, temperature=temperature)  # type: ignore[misc]

    @property
    def last_user_prompt(self) -> str:
        assert self.seen, "provider was never asked to generate"
        return str(self.seen[-1]["user"])

    @property
    def last_system_prompt(self) -> str:
        assert self.seen, "provider was never asked to generate"
        return str(self.seen[-1]["system"])


class SpyMockProvider(_RecordingMixin, MockGemmaProvider):
    """The default development provider, instrumented."""


class SpyRealProvider(_RecordingMixin, GemmaProvider):
    """Stands in for any real, model-backed provider: it inherits `base.py`'s
    `generate_action` unchanged, which is exactly what `OpenAICompatibleGemmaProvider`
    and `TransformersGemmaProvider` do. Only transport differs in the real ones, and
    transport is not what these tests are about."""

    name = "spy_real"

    def __init__(self, response: str) -> None:
        super().__init__()
        self._response = response

    async def _generate(self, system, user, *, images=None, temperature=None):  # type: ignore[no-untyped-def]
        self.seen.append(
            {"system": system, "user": user, "images": list(images or []), "temperature": temperature}
        )
        return self._response


def _valid_action(element_id: str) -> str:
    return json.dumps(
        {
            "action": "click",
            "element_id": element_id,
            "reason": "Opens an unexplored region.",
            "expected_result": "A new page loads.",
            "risk": "low",
            "category": "exploration",
        }
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _element(eid: str, **overrides) -> InteractiveElement:  # type: ignore[no-untyped-def]
    data = {
        "element_id": eid,
        "tag": "a",
        "category": "link",
        "accessible_name": f"Link {eid}",
        "visible_text": f"Link {eid}",
        "href": f"/{eid}",
        "is_visible": True,
        "is_enabled": True,
    }
    data.update(overrides)
    return InteractiveElement(**data)


def _page(*element_ids: str, **overrides) -> PageState:  # type: ignore[no-untyped-def]
    data = {
        "page_id": new_id(),
        "url": "https://app.example.com/home",
        "title": "Home",
        "interactive_elements": [_element(eid) for eid in element_ids],
    }
    data.update(overrides)
    return PageState(**data)


def _request(page: PageState, **overrides) -> ActionGenerationRequest:  # type: ignore[no-untyped-def]
    data = {"page_state": page, "remaining_action_budget": 10, "remaining_page_budget": 5}
    data.update(overrides)
    return ActionGenerationRequest(**data)


# ===========================================================================
# Group A — the real prompt path is exercised without a network model
# ===========================================================================


@pytest.mark.asyncio
async def test_action_generation_reaches_the_real_prompt_builder():
    provider = SpyMockProvider(generate_hook=lambda s, u: _valid_action("el_1"))
    action = await provider.generate_action(_request(_page("el_1", "el_2")))

    assert action.action == ActionType.CLICK
    assert action.element_id == "el_1"
    assert provider.seen, "the real _generate path was never reached"

    user = provider.last_user_prompt
    # These substrings exist only in app/gemma/prompts.py's action payload.
    for marker in ("known_element_ids", "allowed_actions", "Response schema"):
        assert marker in user, f"prompt is missing {marker!r} — build_action_prompt was bypassed"
    # And the system prompt is the centralized template, not something ad hoc.
    assert "GemmaQA" in provider.last_system_prompt


@pytest.mark.asyncio
async def test_prompt_sanitization_is_executed_on_the_action_path():
    """A password value and a secret-shaped memory entry must not survive into the
    serialized prompt. Proves sanitize_page_state_for_model + sanitize_dict ran."""
    page = _page(
        "el_1",
        forms=[
            FormDescriptor(
                form_id="form_1",
                fields=[
                    FormField(
                        name="password",
                        field_type="password",
                        label="Password",
                        current_value="hunter2-super-secret",
                    )
                ],
            )
        ],
    )
    page.interactive_elements.append(
        _element("el_pw", tag="input", category="input", input_type="password", name="password",
                 current_value="hunter2-super-secret")
    )
    provider = SpyMockProvider(generate_hook=lambda s, u: _valid_action("el_1"))
    await provider.generate_action(
        _request(page, memory={"api_key": "sk-live-do-not-leak", "authenticated": True})
    )

    user = provider.last_user_prompt
    assert "hunter2-super-secret" not in user
    assert "sk-live-do-not-leak" not in user
    assert MASK in user


@pytest.mark.asyncio
async def test_prompt_size_handling_is_executed():
    """A page far larger than the prompt budget must still yield a bounded prompt."""
    from app.config import get_settings

    page = _page(*[f"el_{i}" for i in range(400)])
    page.visible_text_summary = "lorem ipsum dolor sit amet " * 4000
    page.headings = [f"Heading number {i} with a fair amount of padding text" for i in range(200)]

    provider = SpyMockProvider(generate_hook=lambda s, u: _valid_action("el_1"))
    await provider.generate_action(_request(page))

    budget = get_settings().max_prompt_chars
    assert len(provider.last_user_prompt) <= budget, "prompt size handling did not run"


@pytest.mark.asyncio
async def test_model_output_parsing_and_validation_are_executed():
    """An invented element_id must be rejected, triggering exactly one correction
    retry, and then the deterministic fallback — proving parse_and_validate_action
    ran rather than the model's word being taken."""
    provider = SpyMockProvider(generate_hook=lambda s, u: _valid_action("el_invented_by_model"))
    action = await provider.generate_action(_request(_page("el_1", "el_2")))

    assert len(provider.seen) == 2, "expected initial attempt + one correction retry"
    assert "corrected JSON" in provider.seen[1]["user"], "correction prompt was not used"
    assert action.element_id != "el_invented_by_model"
    assert (action.metadata or {}).get("ai_failure") is True


@pytest.mark.asyncio
async def test_high_risk_model_output_is_rejected_by_validation():
    high_risk = json.dumps(
        {
            "action": "click",
            "element_id": "el_1",
            "reason": "Delete everything.",
            "expected_result": "Data removed.",
            "risk": "critical",
            "category": "exploration",
        }
    )
    provider = SpyMockProvider(generate_hook=lambda s, u: high_risk)
    action = await provider.generate_action(_request(_page("el_1")))
    assert (action.metadata or {}).get("ai_failure") is True


@pytest.mark.asyncio
async def test_mock_and_real_providers_receive_identical_normalized_context():
    """Provider transport may differ; context meaning must not."""
    page = _page("el_1", "el_2", "el_3")
    memory = {"authenticated": False, "visited_urls": ["https://app.example.com/home"]}

    mock = SpyMockProvider(generate_hook=lambda s, u: _valid_action("el_1"))
    real = SpyRealProvider(_valid_action("el_1"))

    await mock.generate_action(_request(page, memory=dict(memory)))
    await real.generate_action(_request(page, memory=dict(memory)))

    assert mock.last_system_prompt == real.last_system_prompt
    assert mock.last_user_prompt == real.last_user_prompt


@pytest.mark.asyncio
async def test_action_decoding_is_deterministic():
    provider = SpyRealProvider(_valid_action("el_1"))
    await provider.generate_action(_request(_page("el_1")))
    assert provider.seen[-1]["temperature"] == 0.0


# ===========================================================================
# Group B — structured, importance-aware reduction (audit finding J-1)
# ===========================================================================


def test_reduction_keeps_payload_valid_json_on_a_huge_page():
    from app.gemma.prompts import render_action_prompt

    projection = _big_projection(controls=600, text_repeat=3000)
    prompt, report = render_action_prompt(
        projection, known_element_ids=[f"el_{i}" for i in range(600)], max_prompt_chars=6000
    )
    assert len(prompt) <= 6000
    assert report.reduction_applied is True

    # The payload must still parse. Before this work, truncation cut the
    # serialized string and produced exactly this failure. (The prompt also
    # contains the schema hint's own example object, so the payload is located
    # by its header rather than by the first brace.)
    payload = _extract_payload(prompt)
    assert "known_element_ids" in payload


def _extract_payload(prompt: str) -> dict:
    from app.gemma.prompts import _ACTION_PROMPT_HEADER, _ACTION_PROMPT_FOOTER

    body = prompt.split(_ACTION_PROMPT_HEADER, 1)[1]
    body = body.split(_ACTION_PROMPT_FOOTER, 1)[0]
    return json.loads(body)


def test_reduction_preserves_the_security_reminder():
    from app.gemma.prompts import ACTION_PROMPT_REMINDER, render_action_prompt

    prompt, report = render_action_prompt(
        _big_projection(controls=600, text_repeat=3000), max_prompt_chars=3000
    )
    assert ACTION_PROMPT_REMINDER in prompt
    assert "reminder" not in report.omitted_sections


def test_reduction_drops_low_priority_context_before_controls():
    from app.gemma.prompts import render_action_prompt

    _prompt, report = render_action_prompt(
        _big_projection(controls=120, text_repeat=2000), max_prompt_chars=8000
    )
    touched = set(report.reduced_sections) | set(report.omitted_sections)
    # Low-priority history/diagnostics/prose are reduced; the actionable control
    # list is not touched until everything cheaper has been.
    assert touched, "expected some reduction on an over-budget payload"
    low_priority = {"visited_states", "engine_diagnostics", "untrusted_page_detail", "graph_context"}
    assert touched & low_priority, f"expected low-priority reduction first, got {sorted(touched)}"
    assert "untrusted_page_controls" not in report.omitted_sections


def test_reduction_reports_what_it_changed():
    from app.gemma.prompts import render_action_prompt

    _prompt, report = render_action_prompt(_big_projection(controls=400, text_repeat=2000), max_prompt_chars=5000)
    data = report.to_dict()
    for key in ("included_sections", "reduced_sections", "omitted_sections", "estimated_tokens"):
        assert key in data
    assert data["estimated_tokens"] > 0


def test_reduction_is_deterministic():
    from app.gemma.prompts import render_action_prompt

    first, r1 = render_action_prompt(_big_projection(controls=300, text_repeat=1500), max_prompt_chars=7000)
    second, r2 = render_action_prompt(_big_projection(controls=300, text_repeat=1500), max_prompt_chars=7000)
    assert first == second
    assert r1.to_dict() == r2.to_dict()


def test_reduction_converges_even_at_an_absurdly_tight_budget():
    """Found by a live probe: shrinking alone has a floor (every list at one
    item), so without a final drop pass the reducer quietly overran the budget."""
    from app.gemma.prompts import ACTION_PROMPT_REMINDER, action_prompt_floor_chars, render_action_prompt

    floor = action_prompt_floor_chars()
    assert floor < 2000, "the irreducible floor should be a small fraction of any real budget"

    for budget in (2500, 1800, floor):
        prompt, report = render_action_prompt(
            _big_projection(controls=50, text_repeat=500), max_prompt_chars=budget
        )
        assert len(prompt) <= budget, f"overran a {budget}-char budget with {len(prompt)}"
        assert ACTION_PROMPT_REMINDER in prompt, "the security reminder must survive any budget"
        assert report.omitted_sections, "an over-budget payload must report what it dropped"
        _extract_payload(prompt)  # still well-formed JSON


def test_below_the_documented_floor_the_contract_still_survives():
    """A budget smaller than the response schema plus the security reminder cannot
    be honoured. That floor is stated by `action_prompt_floor_chars()` rather than
    discovered as a silent overrun, and what survives is exactly the two things
    that must never be dropped."""
    from app.gemma.prompts import (
        ACTION_PROMPT_REMINDER,
        action_prompt_floor_chars,
        render_action_prompt,
    )

    impossible = action_prompt_floor_chars() // 2
    prompt, _report = render_action_prompt(_big_projection(controls=50, text_repeat=500), max_prompt_chars=impossible)
    assert ACTION_PROMPT_REMINDER in prompt
    assert "Response schema" in prompt
    assert len(prompt) <= action_prompt_floor_chars()
    _extract_payload(prompt)


def test_the_tightest_budgets_drop_low_priority_context_before_controls():
    from app.gemma.prompts import render_action_prompt

    _prompt, report = render_action_prompt(
        _big_projection(controls=50, text_repeat=500), max_prompt_chars=2500
    )
    omitted = set(report.omitted_sections)
    # Controls (priority 6) outlive the evidence registry (7), failures (8),
    # history (9), and everything below.
    if "untrusted_page_controls" in omitted:
        assert {"recent_actions", "visited_states", "engine_diagnostics", "evidence_registry"} <= omitted


def test_no_reduction_when_the_payload_already_fits():
    from app.gemma.prompts import render_action_prompt

    _prompt, report = render_action_prompt(_big_projection(controls=2, text_repeat=1), max_prompt_chars=200_000)
    assert report.reduction_applied is False
    assert report.reduced_sections == []
    assert report.omitted_sections == []


def _big_projection(*, controls: int, text_repeat: int) -> dict:
    return {
        "task_context": {"operator_testing_objective": "Verify the approval workflow end to end."},
        "page_context": {
            "source": "canonical",
            "url": "https://app.example.com/home",
            "title": "Home",
            "controls": [
                {"element_id": f"el_{i}", "role": "button", "accessible_name": f"Control {i}"}
                for i in range(controls)
            ],
            "headings": [f"Heading {i} with padding text" for i in range(200)],
            "visible_text_summary": "lorem ipsum " * text_repeat,
        },
        "application_memory": {"authenticated": False, "auth_status": "unauthenticated"},
        "engine_context": {
            "coverage_gaps": [
                {"gap_id": f"gap_{i}", "gap_type": "unverified_relationship", "description": f"Gap {i}"}
                for i in range(20)
            ],
            "diagnostics": {"knowledge_graph": {"total_nodes": 40, "notes": "x" * 4000}},
        },
        "graph_context": {"nodes": [{"node_id": f"n_{i}", "canonical_name": f"N{i}"} for i in range(40)]},
        "technical_evidence": {"console_error_evidence_ids": [f"ev_c_{i}" for i in range(20)]},
        "constraints": {"safe_mode": True, "authorized_domain": "app.example.com"},
        "evidence_registry": [{"evidence_id": f"ev_{i}", "kind": "dom_subtree", "summary": "s"} for i in range(30)],
        "recent_actions": [{"action": "click", "element_id": f"el_{i}", "success": True} for i in range(40)],
        "visited_states": [f"fp_{i}" for i in range(60)],
        "unexplored_navigation": [f"https://app.example.com/p{i}" for i in range(40)],
        "blocked_reasons": [],
        "projection_metadata": {},
    }


# ===========================================================================
# Group C — operator testing objective, end to end (audit finding J-3)
# ===========================================================================


def test_run_configuration_distinguishes_missing_from_empty_objective():
    from app.schemas import RunConfiguration

    assert RunConfiguration().testing_objective is None
    assert RunConfiguration(testing_objective="").testing_objective == ""
    assert RunConfiguration(testing_objective="Check permissions").testing_objective == "Check permissions"


@pytest.mark.asyncio
async def test_a_provided_objective_reaches_the_action_prompt_verbatim():
    objective = "Confirm a manager can approve a request a clerk submitted."
    provider = SpyRealProvider(_valid_action("el_1"))
    await provider.generate_action(
        _request(_page("el_1"), testing_objective=objective, testing_objective_provided=True)
    )
    assert objective in provider.last_user_prompt


@pytest.mark.asyncio
async def test_a_missing_objective_is_reported_as_missing_not_invented():
    provider = SpyRealProvider(_valid_action("el_1"))
    await provider.generate_action(_request(_page("el_1"), testing_objective_provided=False))
    user = provider.last_user_prompt
    assert "not_provided_by_operator" in user
    assert '"operator_testing_objective": null' in user


def test_objective_reaches_ranking_context_and_states_absence():
    from app.agent.planner import _objective_context

    assert _objective_context({"testing_objective": "Cover the audit trail."}) == {
        "testing_objective": "Cover the audit trail."
    }
    absent = _objective_context({})
    assert absent["testing_objective"] is None
    assert absent["objective_status"] == "not_provided_by_operator"


def test_objective_reaches_the_final_report():
    from app.agent.memory import RunMemory
    from app.schemas import RunConfiguration

    memory = RunMemory(
        run_id="run-1",
        start_url="https://app.example.com",
        configuration=RunConfiguration(testing_objective="Verify approvals."),
    )
    assert memory.testing_objective == "Verify approvals."

    no_objective = RunMemory(run_id="run-2", start_url="https://app.example.com")
    assert no_objective.testing_objective is None


def test_controller_plan_context_carries_the_objective():
    """Guards the exact wiring the audit found broken: plan_context had no such key."""
    import inspect

    from app.agent import controller as controller_module

    source = inspect.getsource(controller_module.AgentController.run)
    assert '"testing_objective": self.config.testing_objective' in source


# ===========================================================================
# Group D — canonical page projection and legacy fallback (audit finding J-4)
# ===========================================================================


def _canonical(*element_ids: str, **overrides):  # type: ignore[no-untyped-def]
    from app.perception.models import CanonicalPageModel, HeadingDescriptor

    data = {
        "url": "https://app.example.com/home",
        "title": "Home",
        "headings": [HeadingDescriptor(stable_id="h1", text="Dashboard")],
        "interactive_elements": [_element(eid) for eid in element_ids],
    }
    data.update(overrides)
    return CanonicalPageModel(**data)


def test_canonical_projection_is_used_when_available():
    from app.gemma.page_projection import PAGE_SOURCE_CANONICAL, project_page

    projection = project_page(page_state=_page("el_1"), canonical_model=_canonical("el_1", "el_2"))
    assert projection["source"] in {PAGE_SOURCE_CANONICAL, "mixed"}
    assert {c["element_id"] for c in projection["controls"]} >= {"el_1", "el_2"}
    assert "Dashboard" in projection["headings"]


def test_legacy_projection_is_used_when_no_canonical_model_exists():
    from app.gemma.page_projection import PAGE_SOURCE_LEGACY, project_page

    projection = project_page(page_state=_page("el_1", "el_2"), canonical_model=None)
    assert projection["source"] == PAGE_SOURCE_LEGACY
    assert {c["element_id"] for c in projection["controls"]} == {"el_1", "el_2"}


def test_a_stale_canonical_model_is_rejected_rather_than_trusted():
    from app.gemma.page_projection import PAGE_SOURCE_LEGACY, project_page

    other_page = _canonical("el_9", url="https://app.example.com/somewhere-else")
    projection = project_page(page_state=_page("el_1"), canonical_model=other_page)
    assert projection["source"] == PAGE_SOURCE_LEGACY
    assert projection["canonical_rejected_reason"] == "url_mismatch"


def test_controls_present_in_both_sources_appear_exactly_once():
    from app.gemma.page_projection import project_page

    page = _page("el_1", "el_2", "el_3")
    projection = project_page(page_state=page, canonical_model=_canonical("el_1", "el_2"))
    ids = [c["element_id"] for c in projection["controls"]]
    assert len(ids) == len(set(ids)), f"duplicate controls: {ids}"
    assert set(ids) == {"el_1", "el_2", "el_3"}


def test_projection_marks_the_source_as_mixed_when_it_blends_both():
    from app.gemma.page_projection import PAGE_SOURCE_MIXED, project_page

    page = _page("el_1", console_errors=["TypeError: undefined is not a function"])
    projection = project_page(page_state=page, canonical_model=_canonical("el_1"))
    assert projection["source"] == PAGE_SOURCE_MIXED
    assert projection["console_errors"]


def test_canonical_projection_stays_bounded():
    from app.gemma.page_projection import MAX_CONTROLS, project_page

    model = _canonical(*[f"el_{i}" for i in range(500)])
    projection = project_page(page_state=_page(), canonical_model=model)
    assert len(projection["controls"]) <= MAX_CONTROLS


def test_canonical_projection_includes_accessibility_and_semantics():
    from app.gemma.page_projection import project_canonical_page
    from app.perception.models import ActionSemantics

    model = _canonical(
        "el_1",
        action_semantics=[ActionSemantics(element_id="el_1", semantic_action="submit")],
    )
    model.interactive_elements[0].role = "button"
    projection = project_canonical_page(model)
    control = projection["controls"][0]
    assert control["role"] == "button"
    assert control["semantic_action"] == "submit"


def test_visual_evidence_appears_only_when_present():
    from app.gemma.page_projection import project_canonical_page
    from app.perception.models import VisualEvidence

    plain = project_canonical_page(_canonical("el_1"))
    assert "visual_evidence" not in plain

    with_visual = project_canonical_page(
        _canonical(
            "el_1",
            visual_evidence=[
                VisualEvidence(element_id="el_1", visual_semantic_type="icon_button", description="A gear icon")
            ],
        )
    )
    assert with_visual["visual_evidence"][0]["element_id"] == "el_1"


@pytest.mark.asyncio
async def test_canonical_controls_are_referenceable_by_the_model():
    """A control that exists only in canonical perception must be selectable —
    otherwise passing canonical data to the model would be a trap."""
    provider = SpyRealProvider(_valid_action("el_canonical_only"))
    action = await provider.generate_action(
        _request(_page("el_1"), canonical_page_model=_canonical("el_1", "el_canonical_only"))
    )
    assert action.element_id == "el_canonical_only"
    assert (action.metadata or {}).get("ai_failure") is not True


# ===========================================================================
# Group E — relevance projection, deduplication, exact gap detail (J-8)
# ===========================================================================


def _ctx(**overrides):  # type: ignore[no-untyped-def]
    from app.gemma.model_context import ModelContext

    return ModelContext(**overrides)


def test_projection_emits_the_documented_section_shape():
    from app.intelligence.knowledge_graph.graph_context_projector import project_model_context

    projection = project_model_context(_ctx())
    for section in (
        "task_context",
        "page_context",
        "engine_context",
        "graph_context",
        "technical_evidence",
        "constraints",
        "evidence_registry",
        "projection_metadata",
    ):
        assert section in projection
    metadata = projection["projection_metadata"]
    for key in ("included_sections", "reduced_sections", "omitted_sections", "estimated_tokens"):
        assert key in metadata


def test_projection_includes_exact_gap_detail_not_just_counts():
    from app.intelligence.knowledge_graph.graph_context_projector import project_model_context

    gaps = [
        {
            "gap_id": "gap_1",
            "gap_type": "unverified_relationship",
            "description": "No observation confirms the recorded ownership relationship.",
            "exploration_value": 0.9,
            "risk": "high",
        }
    ]
    projection = project_model_context(_ctx(coverage_gaps=gaps))
    included = projection["engine_context"]["coverage_gaps"]
    assert included[0]["gap_id"] == "gap_1"
    assert "No observation confirms" in included[0]["description"]


def test_projection_deduplicates_repeated_facts():
    from app.intelligence.knowledge_graph.graph_context_projector import project_model_context

    duplicate = {"gap_id": "gap_1", "gap_type": "x", "description": "same gap"}
    projection = project_model_context(_ctx(coverage_gaps=[duplicate, dict(duplicate), dict(duplicate)]))
    assert len(projection["engine_context"]["coverage_gaps"]) == 1


def test_projection_ranks_higher_value_gaps_first():
    from app.intelligence.knowledge_graph.graph_context_projector import project_model_context

    projection = project_model_context(
        _ctx(
            coverage_gaps=[
                {"gap_id": "low", "description": "low", "exploration_value": 0.1},
                {"gap_id": "high", "description": "high", "exploration_value": 0.95, "risk": "high"},
            ]
        )
    )
    ranked = [g["gap_id"] for g in projection["engine_context"]["coverage_gaps"]]
    assert ranked[0] == "high"


def test_projection_bounds_gap_and_contradiction_counts():
    from app.intelligence.knowledge_graph.graph_context_projector import (
        MODEL_MAX_CONTRADICTIONS,
        MODEL_MAX_GAPS,
        project_model_context,
    )

    projection = project_model_context(
        _ctx(
            coverage_gaps=[{"gap_id": f"g{i}", "description": f"d{i}"} for i in range(100)],
            contradictions=[
                {"contradiction_type": f"t{i}", "description": f"c{i}"} for i in range(100)
            ],
        )
    )
    assert len(projection["engine_context"]["coverage_gaps"]) <= MODEL_MAX_GAPS
    assert len(projection["engine_context"]["unresolved_contradictions"]) <= MODEL_MAX_CONTRADICTIONS


def test_projection_includes_unresolved_contradictions():
    from app.intelligence.knowledge_graph.graph_context_projector import project_model_context

    projection = project_model_context(
        _ctx(
            contradictions=[
                {"contradiction_type": "state_contradiction", "description": "Two states claim to be current."}
            ]
        )
    )
    assert projection["engine_context"]["unresolved_contradictions"][0]["contradiction_type"] == (
        "state_contradiction"
    )


def test_projection_records_provenance_for_every_attributed_section():
    from app.gemma.model_context import ContextProvenance

    context = _ctx()
    context.attribute("coverage_gaps", ContextProvenance(producer="GraphGapAnalyzer", confidence=0.8))
    payload = context.provenance_payload()
    assert payload["coverage_gaps"]["producer"] == "GraphGapAnalyzer"
    assert payload["coverage_gaps"]["confidence"] == 0.8


def test_aggregation_reads_engine_findings_without_recomputing_them():
    """The aggregator must consume `memory_snapshot()` output, not re-derive it."""
    from app.gemma.model_context import build_model_context

    request = _request(
        _page("el_1"),
        memory={
            "active_goal": {"goal_id": "g1", "goal_type": "inspect_form", "confidence": 0.7},
            "knowledge_graph": {"total_nodes": 12, "total_edges": 20},
            "blocked_hrefs": ["https://app.example.com/logout"],
        },
    )
    context = build_model_context(request)
    assert context.active_goal["goal_id"] == "g1"
    assert context.engine_diagnostics["knowledge_graph"]["total_nodes"] == 12
    assert context.blocked_reasons[0]["href"].endswith("/logout")
    assert context.provenance["active_goal"].producer.startswith("GoalGenerationEngine")


# ===========================================================================
# Group F — evidence registry and evidence-id validation (audit finding J-6)
# ===========================================================================


def test_evidence_ids_are_deterministic_and_content_derived():
    from app.gemma.model_context import stable_evidence_id

    first = stable_evidence_id("console_error", "TypeError: x is not a function")
    second = stable_evidence_id("console_error", "TypeError: x is not a function")
    other = stable_evidence_id("console_error", "ReferenceError: y is not defined")
    assert first == second
    assert first != other


def test_registry_is_populated_from_the_current_observation():
    from app.gemma.model_context import EvidenceRegistry
    from app.gemma.evidence_retrieval import retrieve_for_targets, retrieve_technical_evidence

    registry = EvidenceRegistry()
    page = _page("el_1", "el_2", console_errors=["TypeError: boom"], network_failures=["500 /api/x"])
    retrieve_for_targets(registry=registry, target_element_ids=["el_1", "el_2"], page_state=page)
    technical = retrieve_technical_evidence(registry=registry, page_state=page, screenshot_path="/tmp/shot.png")

    assert len(registry) > 0
    assert technical["console_error_evidence_ids"]
    assert technical["network_failure_evidence_ids"]
    assert technical["screenshot_evidence_id"] in registry


def test_retrieval_refuses_unknown_or_stale_references():
    from app.gemma.model_context import EvidenceRegistry
    from app.gemma.evidence_retrieval import retrieve_for_targets

    registry = EvidenceRegistry()
    registered = retrieve_for_targets(
        registry=registry, target_element_ids=["el_does_not_exist"], page_state=_page("el_1")
    )
    assert registered == []
    assert len(registry) == 0


def test_all_valid_evidence_ids_are_kept():
    from app.gemma.model_context import EvidenceRegistry
    from app.gemma.parser import validate_evidence_ids

    registry = EvidenceRegistry()
    a = registry.register(kind="console_error", summary="a").evidence_id
    b = registry.register(kind="console_error", summary="b").evidence_id
    assert validate_evidence_ids([a, b], evidence_registry=registry) == [a, b]


def test_mixed_valid_and_invented_evidence_ids_keeps_only_the_valid_ones():
    from app.gemma.model_context import EvidenceRegistry
    from app.gemma.parser import validate_evidence_ids

    registry = EvidenceRegistry()
    good = registry.register(kind="console_error", summary="a").evidence_id
    kept = validate_evidence_ids([good, "ev_totally_made_up"], evidence_registry=registry)
    assert kept == [good]
    assert registry.rejected_ids == ["ev_totally_made_up"]


def test_entirely_invented_evidence_ids_are_all_dropped():
    from app.gemma.model_context import EvidenceRegistry
    from app.gemma.parser import validate_evidence_ids

    registry = EvidenceRegistry()
    registry.register(kind="console_error", summary="a")
    assert validate_evidence_ids(["nope_1", "nope_2"], evidence_registry=registry) == []


def test_duplicate_evidence_ids_are_deduplicated():
    from app.gemma.model_context import EvidenceRegistry
    from app.gemma.parser import validate_evidence_ids

    registry = EvidenceRegistry()
    good = registry.register(kind="console_error", summary="a").evidence_id
    assert validate_evidence_ids([good, good, good], evidence_registry=registry) == [good]


def test_a_stale_evidence_id_from_an_earlier_observation_is_rejected():
    """Deterministic ids make staleness detectable: an id built from a previous
    observation's content simply is not in the current registry."""
    from app.gemma.model_context import EvidenceRegistry, stable_evidence_id
    from app.gemma.parser import validate_evidence_ids

    earlier = EvidenceRegistry()
    stale_id = earlier.register(kind="console_error", summary="error from the previous page").evidence_id

    current = EvidenceRegistry()
    current.register(kind="console_error", summary="error on the page we are looking at now")

    assert stale_id == stable_evidence_id("console_error", "error from the previous page|")
    assert validate_evidence_ids([stale_id], evidence_registry=current) == []


def test_bug_analysis_never_persists_an_invented_evidence_id():
    from app.gemma.model_context import EvidenceRegistry
    from app.gemma.parser import parse_bug_analysis

    registry = EvidenceRegistry()
    real = registry.register(kind="console_error", summary="TypeError: boom").evidence_id
    raw = json.dumps(
        {
            "classification": "suspected_bug",
            "title": "Console error on load",
            "severity": "medium",
            "priority": "medium",
            "confidence": 0.6,
            "evidence_ids": [real, "ev_hallucinated", "ev_also_fake"],
        }
    )
    result = parse_bug_analysis(raw, evidence_registry=registry)
    assert result.evidence_ids == [real]


def test_bug_analysis_without_a_registry_stays_backward_compatible():
    from app.gemma.parser import parse_bug_analysis

    raw = json.dumps({"classification": "observation", "title": "x", "evidence_ids": ["ev_a", "ev_b"]})
    assert parse_bug_analysis(raw).evidence_ids == ["ev_a", "ev_b"]


@pytest.mark.asyncio
async def test_action_evidence_citations_are_validated():
    cited = json.dumps(
        {
            "action": "click",
            "element_id": "el_1",
            "reason": "Following up the console error.",
            "expected_result": "Detail view opens.",
            "risk": "low",
            "category": "exploration",
            "evidence_ids": ["ev_invented_by_the_model"],
        }
    )
    provider = SpyRealProvider(cited)
    action = await provider.generate_action(_request(_page("el_1")))
    assert "ev_invented_by_the_model" not in (action.metadata or {}).get("evidence_ids", [])


# ===========================================================================
# Group G — sensitive data must never reach serialized model context
# ===========================================================================

SECRETS = {
    "password": "hunter2-super-secret",
    "access_token": "at_live_9f8e7d6c5b4a",
    "refresh_token": "rt_live_1a2b3c4d5e6f",
    "api_key": "sk-live-DO-NOT-LEAK",
    "cookie": "session=abc123def456",
    "authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
    "session_id": "JSESSIONID-0099887766",
    "private_key": "-----BEGIN PRIVATE KEY-----MIIEvQ-----END PRIVATE KEY-----",
    "card_number": "4111111111111111",
}


@pytest.mark.asyncio
async def test_no_known_secret_pattern_reaches_the_serialized_context():
    page = _page(
        "el_1",
        forms=[
            FormDescriptor(
                form_id="form_1",
                fields=[
                    FormField(name=name, field_type="password", label=name, current_value=value)
                    for name, value in SECRETS.items()
                ],
            )
        ],
    )
    provider = SpyRealProvider(_valid_action("el_1"))
    await provider.generate_action(_request(page, memory=dict(SECRETS)))

    user = provider.last_user_prompt
    for name, value in SECRETS.items():
        assert value not in user, f"secret {name!r} leaked into model context"


@pytest.mark.asyncio
async def test_credentials_appear_only_as_capability_metadata():
    provider = SpyRealProvider(_valid_action("el_1"))
    await provider.generate_action(
        _request(
            _page("el_1"),
            memory={
                "credential_profile_available": True,
                "actor_role": "admin",
                "password": "hunter2-super-secret",
            },
        )
    )
    user = provider.last_user_prompt
    assert "credential_profile_available" in user
    assert "hunter2-super-secret" not in user


def test_html_sanitization_strips_scripts_styles_and_secrets():
    from app.gemma.evidence_retrieval import sanitize_html_fragment

    fragment = (
        '<div class="wrapper" data-internal="x" onclick="steal()">'
        '<script>fetch("/exfiltrate")</script>'
        "<style>.a{color:red}</style>"
        '<label for="pw">Password</label>'
        '<input id="pw" name="password" type="password" value="hunter2-super-secret" required '
        'aria-describedby="hint" data-token="at_live_9f8e7d6c5b4a">'
        "<!-- internal note -->"
        "</div>"
    )
    clean = sanitize_html_fragment(fragment)

    assert "script" not in clean.lower()
    assert "fetch(" not in clean
    assert "color:red" not in clean
    assert "onclick" not in clean
    assert "data-internal" not in clean
    assert "hunter2-super-secret" not in clean
    assert "at_live_9f8e7d6c5b4a" not in clean
    assert "internal note" not in clean
    # Semantics a QA reasoner needs are preserved.
    assert 'type="password"' in clean
    assert 'name="password"' in clean
    assert "required" in clean
    assert 'aria-describedby="hint"' in clean
    assert 'for="pw"' in clean


def test_html_sanitization_bounds_size_and_never_ends_mid_tag():
    from app.gemma.evidence_retrieval import sanitize_html_fragment

    fragment = "".join(f'<div role="row"><span role="cell">cell {i}</span></div>' for i in range(500))
    clean = sanitize_html_fragment(fragment, max_chars=400)
    assert len(clean) <= 400 + len(" <!--truncated-->")
    assert clean.endswith("-->") or clean.endswith(">")
    assert not clean.rstrip().endswith("<")


def test_dom_evidence_retrieval_is_bounded_per_call():
    from app.gemma.evidence_retrieval import MAX_TARGETS, retrieve_for_targets
    from app.gemma.model_context import EvidenceRegistry

    registry = EvidenceRegistry()
    page = _page(*[f"el_{i}" for i in range(200)])
    retrieve_for_targets(
        registry=registry, target_element_ids=[f"el_{i}" for i in range(200)], page_state=page
    )
    # At most two evidence records per target (DOM + accessibility).
    assert len(registry) <= MAX_TARGETS * 2


# ===========================================================================
# Group H — provider parity on the normalized path (audit question 20)
# ===========================================================================


@pytest.mark.asyncio
async def test_the_default_mock_provider_no_longer_bypasses_the_pipeline():
    """The exact gap audit question 20 recorded: with no scripted response the
    mock used to short-circuit before any prompt existed."""
    provider = MockGemmaProvider()
    action = await provider.generate_action(_request(_page("el_1", "el_2")))

    assert action is not None
    prepared = provider.last_generation_request
    assert prepared is not None, "the mock provider skipped prepare_generation_request"
    assert "known_element_ids" in prepared.user
    assert prepared.evidence is not None
    assert prepared.reduction is not None


def test_all_providers_share_one_normalized_context_path():
    from app.gemma.base import GemmaProvider
    from app.gemma.mock_provider import MockGemmaProvider as Mock
    from app.gemma.openai_compatible import OpenAICompatibleGemmaProvider
    from app.gemma.transformers_provider import TransformersGemmaProvider

    for cls in (Mock, OpenAICompatibleGemmaProvider, TransformersGemmaProvider):
        assert cls.prepare_generation_request is GemmaProvider.prepare_generation_request, (
            f"{cls.__name__} overrides the shared normalized path"
        )


@pytest.mark.asyncio
async def test_mock_and_real_produce_the_same_normalized_context_for_one_request():
    page = _page("el_1", "el_2")
    canonical = _canonical("el_1", "el_2")

    mock = MockGemmaProvider()
    real = SpyRealProvider(_valid_action("el_1"))

    mock_prepared = mock.prepare_generation_request(
        _request(page, canonical_page_model=canonical, testing_objective="Cover approvals.",
                 testing_objective_provided=True)
    )
    real_prepared = real.prepare_generation_request(
        _request(page, canonical_page_model=canonical, testing_objective="Cover approvals.",
                 testing_objective_provided=True)
    )

    assert mock_prepared.system == real_prepared.system
    assert mock_prepared.user == real_prepared.user
    assert mock_prepared.known_element_ids == real_prepared.known_element_ids
    assert sorted(mock_prepared.evidence.ids) == sorted(real_prepared.evidence.ids)


@pytest.mark.asyncio
async def test_mock_decisions_still_pass_through_real_validation():
    """Deterministic behaviour is preserved, but it is now validated rather than trusted."""
    provider = MockGemmaProvider()
    action = await provider.generate_action(_request(_page("el_1", "el_2")))
    known = provider.last_generation_request.known_element_ids
    if action.element_id:
        assert action.element_id in known


@pytest.mark.asyncio
async def test_scripted_mock_responses_still_use_the_base_retry_path():
    provider = MockGemmaProvider(scripted_responses=[_valid_action("el_1")])
    action = await provider.generate_action(_request(_page("el_1")))
    assert action.element_id == "el_1"


# ===========================================================================
# Group I — architectural invariants that must not regress
# ===========================================================================

APP = BACKEND / "app"


def test_provider_invocation_remains_single_sourced():
    """`_generate` may only be called from app/gemma/. Any engine, controller, or
    route calling a provider directly would fork the funnel."""
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        if path.parent.name == "gemma":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "._generate(" in text or "await self._generate(" in text:
            offenders.append(str(path.relative_to(APP)))
    assert offenders == [], f"provider invocation leaked outside app/gemma/: {offenders}"


def test_prompt_templates_remain_centralized():
    """Every *_SYSTEM_PROMPT lives in app/gemma/prompts.py — one template system."""
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        if path.name == "prompts.py" and path.parent.name == "gemma":
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "_SYSTEM_PROMPT" in stripped and "=" in stripped and "import" not in stripped:
                name = stripped.split("=")[0].strip()
                if name.endswith("_SYSTEM_PROMPT") and " " not in name:
                    offenders.append(f"{path.relative_to(APP)}: {name}")
    assert offenders == [], f"prompt templates defined outside app/gemma/prompts.py: {offenders}"


def test_no_second_context_builder_stack_was_introduced():
    """The audit's central instruction. GraphContextProjector remains the one
    bounded projection/relevance component; nothing parallel replaced it."""
    from app.intelligence.knowledge_graph import graph_context_projector

    assert hasattr(graph_context_projector, "GraphContextProjector")
    assert hasattr(graph_context_projector, "project_model_context")


def test_existing_action_parsing_behaviour_is_unchanged():
    """Backward compatibility of the pre-existing validation contract."""
    from app.gemma.parser import ActionParseError, parse_and_validate_action

    action = parse_and_validate_action(_valid_action("el_1"), known_element_ids={"el_1"})
    assert action.element_id == "el_1"

    with pytest.raises(ActionParseError):
        parse_and_validate_action(_valid_action("el_unknown"), known_element_ids={"el_1"})

    with pytest.raises(ActionParseError):
        parse_and_validate_action(
            json.dumps(
                {
                    "action": "click",
                    "element_id": "el_1",
                    "reason": "r",
                    "expected_result": "e",
                    "risk": "critical",
                    "category": "exploration",
                }
            ),
            known_element_ids={"el_1"},
        )


def test_build_action_prompt_keeps_its_original_signature():
    """Older callers pass loose keyword arguments and must keep working."""
    from app.gemma.prompts import build_action_prompt

    prompt = build_action_prompt(
        page_state=_page("el_1"),
        memory={"authenticated": False},
        recent_actions=[{"action": "click", "element_id": "el_1", "success": True}],
        visited_states=["fp_1"],
        unexplored=["https://app.example.com/x"],
        testing_objective="Check the login flow.",
        remaining_action_budget=5,
        remaining_page_budget=3,
        safe_mode=True,
        authorized_domain="app.example.com",
        max_prompt_chars=24000,
    )
    assert isinstance(prompt, str)
    assert "Check the login flow." in prompt
    assert "el_1" in prompt


# ===========================================================================
# Group J — reporting exposes context metadata, safely
# ===========================================================================


def test_memory_records_and_summarizes_context_metadata():
    from app.agent.memory import RunMemory

    memory = RunMemory(run_id="run-1", start_url="https://app.example.com")
    memory.record_model_context(
        {
            "page_source": "canonical",
            "decision_path": "model",
            "reduced_sections": ["visited_states"],
            "omitted_sections": ["graph_context"],
            "estimated_tokens": 4200,
            "evidence_items": 12,
            "gaps_included": 3,
            "rejected_evidence_ids": 2,
            "reduction_applied": True,
            "provider": "mock",
        }
    )
    memory.record_model_context(
        {"page_source": "legacy_fallback", "decision_path": "model", "estimated_tokens": 900}
    )
    memory.record_model_context({"decision_path": "deterministic_frontier"})

    summary = memory.model_context_summary()
    assert summary["planning_iterations_recorded"] == 3
    assert summary["model_calls_recorded"] == 2
    assert summary["decision_path_counts"] == {"model": 2, "deterministic_frontier": 1}
    assert summary["page_source_counts"] == {"canonical": 1, "legacy_fallback": 1}
    assert summary["sections_reduced_counts"] == {"visited_states": 1}
    assert summary["sections_omitted_counts"] == {"graph_context": 1}
    assert summary["invalid_evidence_ids_rejected"] == 2
    assert summary["reduction_applied_count"] == 1


def test_context_metadata_history_is_bounded():
    from app.agent.memory import RunMemory

    memory = RunMemory(run_id="run-1", start_url="https://app.example.com")
    for i in range(500):
        memory.record_model_context({"page_source": "canonical", "estimated_tokens": i})
    assert len(memory.model_context_events) == RunMemory.MAX_MODEL_CONTEXT_EVENTS


def test_context_metadata_contains_no_prompt_text_or_secrets():
    provider = MockGemmaProvider()
    prepared = provider.prepare_generation_request(
        _request(_page("el_1"), memory={"api_key": "sk-live-DO-NOT-LEAK"})
    )
    metadata = prepared.context_metadata()
    serialized = json.dumps(metadata)
    assert "sk-live-DO-NOT-LEAK" not in serialized
    # Counts and names only — never the prompt itself.
    assert prepared.user not in serialized
    assert isinstance(metadata["prompt_chars"], int)


def test_empty_context_summary_when_nothing_was_recorded():
    from app.agent.memory import RunMemory

    memory = RunMemory(run_id="run-1", start_url="https://app.example.com")
    assert memory.model_context_summary() == {}


# ===========================================================================
# Group K — end-to-end integration: a real browser, the real controller,
# the real report. Fixtures prove the units; this proves the wiring.
# ===========================================================================

import asyncio  # noqa: E402
import threading  # noqa: E402
from functools import partial  # noqa: E402
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_demo"


@pytest.fixture(scope="module")
def demo_server():
    handler = partial(SimpleHTTPRequestHandler, directory=str(FIXTURES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_a_real_run_records_context_metadata_and_the_objective(demo_server):
    """The whole pipeline, in a real run: objective in, context built from real
    observations, metadata out through the report — with no secrets in it."""
    from app.agent.controller import AgentController
    from app.database import AsyncSessionLocal, init_db
    from app.schemas import CreateRunRequest, RunConfiguration

    objective = "Confirm the primary navigation reaches every listed section."
    asyncio.run(init_db())

    request = CreateRunRequest(
        url=f"{demo_server}/index.html",
        configuration=RunConfiguration(
            max_pages=2,
            max_actions=4,
            safe_mode=True,
            headless=True,
            testing_objective=objective,
        ),
    )
    run_id = new_id()

    async def on_event(event_type: str, data: dict) -> None:
        return None

    controller = AgentController(
        run_id=run_id,
        request=request,
        db_factory=AsyncSessionLocal,
        on_event=on_event,
        gemma=MockGemmaProvider(),
    )

    async def seed_and_run():
        from app.models import QARun

        async with AsyncSessionLocal() as session:
            session.add(
                QARun(
                    id=run_id,
                    url=request.url,
                    status="created",
                    config_json=request.configuration.model_dump_json(),
                )
            )
            await session.commit()
        return await controller.run()

    asyncio.run(seed_and_run())
    memory = controller.memory

    # The objective survived the whole way through configuration.
    assert memory.testing_objective == objective

    # Every planning iteration is accounted for, and the report says which of
    # them were model-driven versus planned deterministically from the frontier.
    summary = memory.model_context_summary()
    assert summary, "no planning metadata was recorded during a real run"
    assert summary["planning_iterations_recorded"] >= 1
    assert summary["decision_path_counts"], "decision path was never recorded"
    assert set(summary["decision_path_counts"]) <= {"model", "deterministic_frontier", "unknown"}
    assert set(summary["page_source_counts"]) <= {"canonical", "legacy_fallback", "mixed"}
    # Where the model WAS consulted, real prompt construction is proven.
    if summary["model_calls_recorded"]:
        assert summary["page_source_counts"], "a model call recorded no page source"

    # And it is safe to publish: counts and names only.
    serialized = json.dumps(summary)
    for banned in ("password", "Bearer ", "Set-Cookie", "<html", "<script"):
        assert banned not in serialized

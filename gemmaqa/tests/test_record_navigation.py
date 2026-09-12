"""Reaching an individual record — the prerequisite for update and delete.

Update and delete controls live inside a record, not on the list that contains
it. Observed live on a record-management application: the list rendered one row
per record, native table rows were given no element id at all, and nothing
offered "open this row" — so the run could reach neither the record nor its
edit/delete controls, and update/delete stayed at zero DISCOVERED for the whole
run.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.frontier import (  # noqa: E402
    MAX_HYPOTHESIS_ROWS_PER_COLLECTION,
    FrontierBuilder,
)
from app.agent.temporary_record_registry import TemporaryRecordRegistry  # noqa: E402
from app.perception.models import (  # noqa: E402
    CanonicalPageModel,
    CollectionRow,
    RecordCollection,
)


class _Memory:
    """Minimal stand-in for the parts of RunMemory these builders read."""

    def __init__(self, registry=None) -> None:
        self.temporary_record_registry = registry
        self.action_signatures: set[str] = set()
        self.signature_counts: dict[str, int] = {}

    def has_seen_signature(self, signature: str) -> bool:
        return signature in self.action_signatures


def _row(element_id: str, cells: list[str], *, index: int = 0, basis: str = "structural_hypothesis") -> CollectionRow:
    return CollectionRow(
        stable_id=element_id,
        element_id=element_id,
        row_index=index,
        cell_values=cells,
        identity_hint=cells[0] if cells else None,
        is_activatable=basis != "none",
        activation_basis=basis,
        activation_evidence=["record_row_shape"] if basis == "structural_hypothesis" else ["cursor_pointer"],
    )


def _model(rows: list[CollectionRow]) -> CanonicalPageModel:
    return CanonicalPageModel(
        url="https://app.example.com/records",
        state_fingerprint="fp_list",
        collections=[
            RecordCollection(
                stable_id="table_003",
                element_id="table_003",
                collection_type="native_table",
                row_count=len(rows),
                visible_rows=rows,
            )
        ],
    )


def _candidates(model, memory):
    return FrontierBuilder._build_record_row_candidates(model, memory, "fp_list")


# ===========================================================================
# A — a row must be reachable at all
# ===========================================================================


def test_an_activatable_row_produces_an_open_candidate():
    cands = _candidates(_model([_row("el_007", ["Ada Lovelace", "ada@example.com"])]), _Memory())
    assert len(cands) == 1
    assert cands[0].candidate_type == "open_record_row"
    assert cands[0].element_id == "el_007"
    assert cands[0].action == "click"


def test_a_row_with_no_element_id_produces_nothing():
    """Native table rows used to arrive with element_id=None, which is exactly
    why no candidate could ever be built for them."""
    row = _row("el_007", ["Ada"])
    row.element_id = None
    assert _candidates(_model([row]), _Memory()) == []


def test_a_row_carrying_its_own_controls_is_not_offered():
    """There the buttons are the actions; the row itself usually is not clickable."""
    assert _candidates(_model([_row("el_007", ["Ada"], basis="none")]), _Memory()) == []


def test_an_already_attempted_row_is_not_offered_again():
    memory = _Memory()
    model = _model([_row("el_007", ["Ada"])])
    first = _candidates(model, memory)
    assert first
    from app.agent.memory import action_signature

    memory.action_signatures.add(
        action_signature(page_fingerprint="fp_list", action_type="click", element_id="el_007")
    )
    assert _candidates(model, memory) == []


# ===========================================================================
# B — the record this run created comes first
# ===========================================================================


def _registry_with(identity: str) -> TemporaryRecordRegistry:
    registry = TemporaryRecordRegistry("run_1")
    registry.register_created(record_type="email", generated_identity=identity)
    return registry


def test_the_row_matching_a_created_record_outranks_the_others():
    rows = [
        _row("el_007", ["Someone Else", "other@example.com"], index=0),
        _row("el_008", ["Ada Lovelace", "ada_qa_42@example.com"], index=1),
    ]
    memory = _Memory(_registry_with("ada_qa_42@example.com"))
    by_id = {c.element_id: c for c in _candidates(_model(rows), memory)}

    assert by_id["el_008"].priority < by_id["el_007"].priority  # lower == preferred
    assert by_id["el_008"].metadata["created_record_identity"] == "ada_qa_42@example.com"
    assert by_id["el_007"].metadata["created_record_identity"] == ""


def test_identity_matching_tolerates_reformatting_by_the_list():
    """A list often renders a truncated or recombined version of what was
    submitted, so exact equality would miss the match."""
    rows = [_row("el_008", ["Ada Lovelace", "Ada Lovelace ada_qa_42@example.com (active)"])]
    memory = _Memory(_registry_with("ada_qa_42@example.com"))
    cands = _candidates(_model(rows), memory)
    assert cands[0].metadata["created_record_identity"] == "ada_qa_42@example.com"


def test_a_very_short_identity_is_not_matched_by_accident():
    rows = [_row("el_008", ["Ada Lovelace"])]
    memory = _Memory(_registry_with("ad"))
    assert _candidates(_model(rows), memory)[0].metadata["created_record_identity"] == ""


def test_no_registry_still_offers_rows():
    """A run that created nothing must still be able to read records."""
    cands = _candidates(_model([_row("el_007", ["Ada"])]), _Memory(registry=None))
    assert len(cands) == 1
    assert cands[0].metadata["created_record_identity"] == ""


# ===========================================================================
# C — an unproven hypothesis must stay bounded
# ===========================================================================


def test_speculative_rows_are_capped_per_collection():
    """Frameworks attach click handlers in JavaScript, leaving no DOM trace, so
    openability is often only a hypothesis. Testing it is cheap and read-only —
    but a hundred-row grid must not become a hundred speculative clicks."""
    rows = [_row(f"el_{i:03d}", [f"Record {i}"], index=i) for i in range(40)]
    cands = _candidates(_model(rows), _Memory())
    assert len(cands) == MAX_HYPOTHESIS_ROWS_PER_COLLECTION


def test_a_created_record_row_is_exempt_from_the_speculative_cap():
    """Opening our own record both verifies the create and is the only route to
    that record's update/delete controls, so it is always worth the action."""
    rows = [_row(f"el_{i:03d}", [f"Record {i}"], index=i) for i in range(40)]
    rows.append(_row("el_999", ["Ours", "mine_qa_7@example.com"], index=99))
    memory = _Memory(_registry_with("mine_qa_7@example.com"))

    cands = _candidates(_model(rows), memory)
    offered = {c.element_id for c in cands}
    assert "el_999" in offered
    assert len(cands) == MAX_HYPOTHESIS_ROWS_PER_COLLECTION + 1


def test_proven_rows_are_not_subject_to_the_speculative_cap():
    rows = [_row(f"el_{i:03d}", [f"Record {i}"], index=i, basis="observed") for i in range(10)]
    cands = _candidates(_model(rows), _Memory())
    assert len(cands) == 10
    assert all(c.metadata["activation_basis"] == "observed" for c in cands)


def test_a_proven_row_outranks_a_merely_presumed_one():
    rows = [
        _row("el_007", ["Presumed"], index=0, basis="structural_hypothesis"),
        _row("el_008", ["Proven"], index=1, basis="observed"),
    ]
    by_id = {c.element_id: c for c in _candidates(_model(rows), _Memory())}
    assert by_id["el_008"].priority < by_id["el_007"].priority


# ===========================================================================
# D — the candidate is safe and dispatchable
# ===========================================================================


def test_opening_a_record_is_read_only():
    cand = _candidates(_model([_row("el_007", ["Ada"])]), _Memory())[0]
    assert cand.risk == "read_only"
    assert cand.semantic_operation == "read"


def test_the_planner_dispatches_the_candidate_as_a_click():
    from app.agent.auth_strategy import AuthenticationStrategy
    from app.agent.memory import RunMemory
    from app.agent.planner import Planner
    from app.gemma.mock_provider import MockGemmaProvider
    from app.schemas import ActionType, PageState

    cand = _candidates(_model([_row("el_007", ["Ada"])]), _Memory())[0]
    planner = Planner(MockGemmaProvider())
    action = planner._candidate_to_action(
        cand,
        PageState(page_id="p", url="https://app.example.com/records"),
        RunMemory(run_id="r", start_url="https://app.example.com"),
        AuthenticationStrategy(),
        {},
    )
    assert action is not None
    assert action.action == ActionType.CLICK
    assert action.element_id == "el_007"


def test_row_activation_reaches_the_canonical_model_from_raw_extraction():
    """The extractor -> model -> frontier chain, so a future change to any one
    of the three cannot silently drop the signal."""
    from app.perception.collection_extractor import _rows_from_raw

    rows = _rows_from_raw(
        [
            {
                "dom_id": "el_007",
                "row_index": 0,
                "cell_values": ["Ada"],
                "activatable": True,
                "activation_basis": "structural_hypothesis",
                "activation_evidence": ["record_row_shape"],
            }
        ],
        prefix="table_003",
        base_confidence=0.9,
    )
    assert rows[0].element_id == "el_007"
    assert rows[0].is_activatable is True
    assert rows[0].activation_basis == "structural_hypothesis"

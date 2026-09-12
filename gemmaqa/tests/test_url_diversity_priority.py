"""open_url candidate diversity (Phase 14 live-acceptance finding): a live
ServiceFlow run never reached the Jobs section because every open_url candidate
shared one uniform priority, tie-broken by an arbitrary URL hash. As Customers
pages kept discovering more same-area candidate URLs (cust-002, cust-003, ...),
"Jobs" — an entirely different, still-unvisited section — could lose that hash
lottery indefinitely. The first candidate URL for a brand-new top-level area must
outrank another candidate in an area already being explored."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.auth_strategy import AuthenticationStrategy  # noqa: E402
from app.agent.frontier import FrontierBuilder  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.schemas import PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402

ROOT = "http://127.0.0.1:5500"


def _memory(*, visited: list[str] | None = None) -> RunMemory:
    memory = RunMemory(run_id=new_id(), start_url=f"{ROOT}/")
    memory.remaining_action_budget = 30
    for u in visited or []:
        memory.visited_urls.add(u)
    return memory


def _page() -> PageState:
    return PageState(page_id=new_id(), url=f"{ROOT}/customers", title="Customers", state_fingerprint="fp-1")


def test_url_in_brand_new_area_outranks_url_in_already_visited_area():
    memory = _memory(visited=[f"{ROOT}/customers", f"{ROOT}/customers/cust-002"])
    unexplored = [
        f"{ROOT}/customers/cust-003",
        f"{ROOT}/customers/cust-004",
        f"{ROOT}/jobs",
    ]
    cands = FrontierBuilder(AuthenticationStrategy()).build(
        _page(), unexplored_urls=unexplored, memory=memory
    )
    by_url = {c.url: c for c in cands if c.candidate_type == "open_url"}
    assert by_url[f"{ROOT}/jobs"].priority < by_url[f"{ROOT}/customers/cust-003"].priority
    assert by_url[f"{ROOT}/jobs"].priority < by_url[f"{ROOT}/customers/cust-004"].priority


def test_only_the_first_new_area_url_gets_the_boost_not_every_candidate_in_it():
    """Two brand-new unvisited URLs in the SAME new area must not both jump the
    queue — only the first one seen this call gets the diversity boost, otherwise
    the boost is meaningless (everything in a fresh area would tie again)."""
    memory = _memory(visited=[f"{ROOT}/customers"])
    unexplored = [f"{ROOT}/jobs", f"{ROOT}/jobs/job-1"]
    cands = FrontierBuilder(AuthenticationStrategy()).build(
        _page(), unexplored_urls=unexplored, memory=memory
    )
    by_url = {c.url: c for c in cands if c.candidate_type == "open_url"}
    priorities = sorted(c.priority for c in by_url.values())
    assert priorities[0] < priorities[1], "only one candidate in the new area should get the boost"


def test_no_boost_when_area_already_visited():
    memory = _memory(visited=[f"{ROOT}/customers", f"{ROOT}/customers/cust-001"])
    unexplored = [f"{ROOT}/customers/cust-002"]
    cands = FrontierBuilder(AuthenticationStrategy()).build(
        _page(), unexplored_urls=unexplored, memory=memory
    )
    cand = next(c for c in cands if c.candidate_type == "open_url")
    assert cand.priority == 30


def test_diversity_boost_does_not_override_structural_priority_band():
    """The boosted priority (29) must still stay firmly inside the "general
    navigation candidate" band — never anywhere near auth/structural priorities
    (1-21) or below them."""
    memory = _memory()
    cands = FrontierBuilder(AuthenticationStrategy()).build(
        _page(), unexplored_urls=[f"{ROOT}/jobs"], memory=memory
    )
    cand = next(c for c in cands if c.candidate_type == "open_url")
    assert cand.priority == 29
    assert cand.priority > 21

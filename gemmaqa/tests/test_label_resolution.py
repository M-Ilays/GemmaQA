"""A field's caption is on the page even when HTML does not declare it.

Operator-reported: GemmaQA typed `GemmaQA_TEST_96259817_7` into an Email box and
`GemmaQA_TEST_96259817_8` into a Contact Number box, and OrangeHRM rejected both
("Expected format: admin@example.com", "Allows numbers and only + - / ( )").

Both extractors' `labelFor` handled only the three cases HTML makes explicit:
`label[for=id]`, a wrapping `<label>`, and `aria-labelledby`. Probed against the
real Add Candidate form, ALL TEN fields had none of them:

    field                 name/id      placeholder      labelFor() saw
    Full Name (x3)        firstName…   "First Name"     nothing
    Email                 -            "Type here"      nothing
    Contact Number        -            "Type here"      nothing
    Date of Application   -            "yyyy-dd-mm"     nothing

So the semantic classifier saw the blob "Type here" for the Email box, matched
no pattern, and fell through to `free_text`. The name fields classified only by
accident, because they carry `name="firstName"` and a useful placeholder.

The captions were ordinary `<label>` elements one to three ancestors up. The fix
reads them the way a person does — see `app.browser.label_resolution`.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app.agent.field_constraint_inference import infer_semantic_type  # noqa: E402
from app.browser.label_resolution import (  # noqa: E402
    MAX_LABEL_ANCESTOR_DEPTH,
    label_resolution_js,
)

# OrangeHRM's shape, reproduced: no `for`, no `id`, no `name`, no `aria-label`,
# a useless placeholder, and the caption in a sibling <label> inside a wrapper.
ORANGEHRM_SHAPED = """
<!doctype html><html><body>
  <form>
    <div class="input-group">
      <label>Full Name</label>
      <div class="row">
        <div class="col"><input placeholder="First Name" name="firstName"></div>
        <div class="col"><input placeholder="Middle Name" name="middleName"></div>
      </div>
    </div>
    <div class="input-group">
      <label>Email</label>
      <div class="field"><input placeholder="Type here"></div>
    </div>
    <div class="input-group">
      <label>Contact Number</label>
      <div class="field"><input placeholder="Type here"></div>
    </div>
    <div class="input-group">
      <label>Date of Application</label>
      <div class="row"><div class="col"><input placeholder="yyyy-dd-mm"></div></div>
    </div>
  </form>
  <button>Export Data</button>
</body></html>
"""


async def _fields(html: str):
    """Observe `html` with the real PageObserver and return its form fields."""
    try:
        from playwright.async_api import async_playwright
    except Exception:  # pragma: no cover - environment without playwright
        pytest.skip("Playwright not importable")

    from app.browser.observer import PageObserver

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(
                    "**/*",
                    lambda r: r.fulfill(status=200, content_type="text/html", body=html),
                )
                await page.goto("https://label-demo.local/form")
                state = await PageObserver().observe(page)
            finally:
                await browser.close()
    except Exception as exc:  # pragma: no cover - no browser in this environment
        pytest.skip(f"Live Chromium unavailable: {type(exc).__name__}: {exc}")
    return state


# ===========================================================================
# A -- the caption is found where a person reads it
# ===========================================================================


@pytest.mark.asyncio
async def test_a_label_with_no_for_attribute_is_still_read():
    state = await _fields(ORANGEHRM_SHAPED)
    labels = [(f.label or "") for form in state.forms for f in form.fields]

    assert "Email" in labels
    assert "Contact Number" in labels


@pytest.mark.asyncio
async def test_a_caption_three_ancestors_up_is_still_read():
    """Full Name and Date of Application sit at depth 3. A limit of 3 stopped one
    short of both — the date field was filled with a random token."""
    state = await _fields(ORANGEHRM_SHAPED)
    labels = [(f.label or "") for form in state.forms for f in form.fields]

    assert "Date of Application" in labels
    assert "Full Name" in labels


@pytest.mark.asyncio
async def test_the_fields_now_classify_to_the_types_the_app_validates():
    """The whole point: the value typed must be the kind of value the field
    accepts. This is the operator's screenshot, as an assertion."""
    state = await _fields(ORANGEHRM_SHAPED)
    by_label = {
        (f.label or ""): infer_semantic_type(f)
        for form in state.forms for f in form.fields
    }

    assert by_label.get("Email") == "email"
    assert by_label.get("Contact Number") == "phone_number"
    assert by_label.get("Date of Application") == "date"


# ===========================================================================
# B -- the fallback must not reach where it does not belong
# ===========================================================================


@pytest.mark.asyncio
async def test_a_button_never_borrows_a_nearby_label():
    """Caught by the existing actor-discovery test while this was being written:
    ungated, `<button>Export Data</button>` walked up to <body>, found the
    unrelated `<label>Full Name</label>`, and took "Full Name" as its accessible
    name — destroying the permission derived from its real text."""
    state = await _fields(ORANGEHRM_SHAPED)
    buttons = [el for el in state.interactive_elements if (el.tag or "").lower() == "button"]

    assert buttons, "the fixture's button was not observed at all"
    for button in buttons:
        assert "Export Data" in (button.accessible_name or ""), button.accessible_name
        assert "Full Name" not in (button.accessible_name or "")
        assert "Email" not in (button.accessible_name or "")


LOGIN_SHAPED = """
<!doctype html><html><body>
  <form>
    <input type="hidden" name="_token" value="csrf">
    <div class="input-group">
      <label>Username</label>
      <div class="field"><input name="username"></div>
    </div>
    <div class="input-group">
      <label>Password</label>
      <div class="field"><input type="password" name="password"></div>
    </div>
    <button type="submit">Login</button>
  </form>
</body></html>
"""


@pytest.mark.asyncio
async def test_a_hidden_field_is_never_given_a_caption():
    """Caught live and only live. OrangeHRM's login form carries a hidden
    `_token` beside the username group; the walk handed it the label
    "Username", the form then looked like it had two username fields, the CSRF
    token was overwritten, and every login failed with
    `unresolved_authentication`. A person reads no caption for a field they
    cannot see, so neither may this."""
    state = await _fields(LOGIN_SHAPED)
    by_name = {
        (f.name or ""): (f.label or "")
        for form in state.forms for f in form.fields
    }

    assert by_name.get("_token", "") == "", by_name
    assert by_name.get("username") == "Username"
    assert by_name.get("password") == "Password"


def test_invisibility_is_checked_before_the_walk_runs():
    js = label_resolution_js(max_chars=120)
    assert "el.type === 'hidden'" in js
    assert js.index("el.type === 'hidden'") < js.index("el.parentElement")


def test_only_form_controls_use_the_ancestor_walk():
    js = label_resolution_js(max_chars=120)
    assert "tag !== 'input' && tag !== 'select' && tag !== 'textarea'" in js
    # The gate must come BEFORE the walk, or the walk still runs for buttons.
    assert js.index("tag !== 'input'") < js.index("el.parentElement")


def test_a_caption_containing_another_control_is_not_a_caption():
    """That is a sibling field's group, not this field's label."""
    js = label_resolution_js(max_chars=120)
    assert "cap.querySelector('input, select, textarea')" in js


def test_a_container_wrapping_the_field_is_not_its_caption():
    """Its text includes the field itself, so it describes nothing."""
    assert "cap.contains(el)" in label_resolution_js(max_chars=120)


# ===========================================================================
# C -- one implementation, two extractors
# ===========================================================================


def test_both_extractors_share_the_generated_helper():
    """Two copies of `labelFor` is how this bug survived: the rule had two homes
    and neither was fixed. Four bugs this session came from that shape."""
    from app.browser.observer import OBSERVE_SCRIPT
    from app.perception.dom_extractor import RAW_EXTRACTION_SCRIPT

    for script in (OBSERVE_SCRIPT, RAW_EXTRACTION_SCRIPT):
        assert "__LABEL_FOR__" not in script, "placeholder was never substituted"
        assert script.count("const labelFor") == 1
        assert "MAX_LABEL_ANCESTOR_DEPTH" not in script  # interpolated, not literal
        assert f"depth < {MAX_LABEL_ANCESTOR_DEPTH}" in script


def test_the_rule_has_exactly_one_definition():
    root = BACKEND / "app"
    definitions = [
        f"{p.relative_to(root)}:{i}"
        for p in root.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if line.startswith("def label_resolution_js(")
    ]
    assert len(definitions) == 1, definitions


def test_the_two_extractors_differ_only_in_truncation_length():
    short, long = label_resolution_js(max_chars=120), label_resolution_js(max_chars=160)
    assert short.replace("120", "N") == long.replace("160", "N")


# ===========================================================================
# D -- a field that cannot be filled must not cost the fields after it
# ===========================================================================
#
# Operator-reported from the screenshot: Resume, Keywords and Notes were left
# empty on a form GemmaQA submitted. From that run's log:
#
#   532  EXEC fill  el_026  ok=False   Locator.fill: Timeout 15000ms exceeded
#                                      <input type="file"> — element is not visible
#   546  PLAN click el_032  Submit safe test data / continue form
#
# Two bugs met. `fill()` cannot set a file input and applications hide the real
# one behind a styled button, so it burnt its whole 15s timeout; and the failure
# was routed through `note_result(success=False)`, which is the SUBMIT path and
# sets `state = "ready_to_submit"` — skipping every remaining field.


def _workflow_mid_fill():
    from app.agent.form_workflow import GenericFormWorkflow

    wf = GenericFormWorkflow.__new__(GenericFormWorkflow)
    wf.form_id = "form_034"
    wf.fields = []
    wf.state = "filling"
    wf.attempts = 0
    wf.last_error = None
    wf.blocked_reason = ""
    wf.failed_field_ids = []
    return wf


def test_one_unfillable_field_leaves_the_form_still_filling():
    wf = _workflow_mid_fill()
    wf.note_field_failed("el_026")

    assert wf.state == "filling", "the remaining fields must still be reached"
    assert wf.attempts == 0
    assert wf.failed_field_ids == ["el_026"]


def test_a_failed_field_never_jumps_the_form_to_submit():
    """The exact defect: `ready_to_submit` here means submitting half a form."""
    wf = _workflow_mid_fill()
    wf.note_field_failed("el_026")

    assert wf.state != "ready_to_submit"


def test_enough_failed_fields_still_abandons_the_form():
    """Continuing must not become never giving up."""
    from app.agent.form_workflow import MAX_FAILED_FIELDS

    wf = _workflow_mid_fill()
    for i in range(MAX_FAILED_FIELDS):
        wf.note_field_failed(f"el_{i:03d}")

    assert wf.state == "failed"
    assert "abandoned" in wf.blocked_reason


def test_the_same_field_is_only_counted_once():
    wf = _workflow_mid_fill()
    wf.note_field_failed("el_026")
    wf.note_field_failed("el_026")

    assert wf.failed_field_ids == ["el_026"]
    assert wf.state == "filling"


def test_the_controller_routes_a_failed_fill_to_the_field_path():
    import inspect

    from app.agent.controller import AgentController

    source = inspect.getsource(AgentController.run)
    assert "note_field_failed(action.element_id" in source
    # The submit path must no longer be reached by a failed FILL.
    fill_branch = source[source.index("UNSATISFIED_REFERENCE_MARKER in (result.error"):]
    assert "note_result(success=False)" not in fill_branch[:600]


def test_a_file_input_is_uploaded_not_typed_into():
    """`fill()` cannot set a file input, and the real one is usually hidden
    behind a styled button — so it waits out its visibility timeout and fails."""
    import inspect

    from app.browser import custom_controls

    source = inspect.getsource(custom_controls.fill_or_choose)
    assert "set_input_files" in source
    # It must be decided BEFORE the chooser/plain-fill paths.
    assert source.index("_is_file_input") < source.index("custom_control_kind")


def test_a_decorated_checkbox_is_ticked_without_the_hit_target_check():
    """A decorated checkbox is not hidden — it is COVERED. Playwright said so:

        element is visible, enabled and stable
        <i class="oxd-icon bi-check"> from <span class="oxd-checkbox-input">
        subtree intercepts pointer events

    A first attempt at this gated on `is_visible()`, which returns True here, so
    the fallback never ran and the tick still burnt 15s. The normal path must be
    tried and its FAILURE is the signal — visibility cannot detect interception.
    """
    import inspect

    from app.browser import custom_controls

    source = inspect.getsource(custom_controls.check_or_force)
    assert "force=True" in source
    # Normal attempt first, forced only on failure.
    assert source.index("except Exception") < source.index("force=True")
    # And the normal attempt must not cost Playwright's full default timeout.
    assert "timeout=CHECK_TIMEOUT_MS" in source
    assert custom_controls.CHECK_TIMEOUT_MS <= 5000


def test_both_execution_paths_tick_checkboxes_the_same_way():
    import inspect

    from app.browser.adapters.direct import DirectPlaywrightAdapter
    from app.browser.executor import ActionExecutor

    assert "check_or_force" in inspect.getsource(ActionExecutor._perform_element_action)
    assert "check_or_force" in inspect.getsource(DirectPlaywrightAdapter.check_target)

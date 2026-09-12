"""How a form field's caption is read off the page. One implementation.

Both injected extractor scripts (`app.browser.observer` and
`app.perception.dom_extractor`) had their own `labelFor`, and both handled only
the three cases HTML makes explicit: `label[for=id]`, a wrapping `<label>`, and
`aria-labelledby`. Plenty of real applications use none of them.

Measured on OrangeHRM's Recruitment -> Add Candidate, every field on the form:

    field                 name/id      placeholder      labelFor() saw
    Full Name (x3)        firstName…   "First Name"     nothing
    Email                 -            "Type here"      nothing
    Contact Number        -            "Type here"      nothing
    Date of Application   -            "yyyy-dd-mm"     nothing
    Resume / Keywords /   -            …                nothing
    Notes / Consent

Ten fields, ten misses. The captions are ordinary `<label>` elements one to
three ancestors up, inside each field's own input group — exactly what a person
reads. Without them the semantic classifier saw the blob `"Type here"` for the
Email box, matched nothing, fell through to `free_text`, and typed
`GemmaQA_TEST_<run>_7` into a field the application validates as an email
address. The name fields only classified correctly by accident, because they
happen to carry `name="firstName"` and a useful placeholder.

The rule added here: **when HTML declares no label, take the caption from the
nearest ancestor that has one.** Nearest, so a field takes its own group's
caption rather than a neighbour's; bounded, so a caption is never dragged in
from halfway up the page.

This module exists so the rule has ONE home. Four bugs earlier in this session
came from a rule with two implementations where only one sat on the path being
measured — including these two extractors, twice.
"""

from __future__ import annotations

# How far up to walk, measured from the field's own parent (depth 0). 4 is what
# the page actually needs: measured on Add Candidate, Email sits at depth 1 but
# Full Name and Date of Application sit at depth 3, and a limit of 3 stopped one
# short of both — "Date of Application" stayed unresolved and its field was
# filled with a random token. Higher would start borrowing captions across
# sibling fields.
MAX_LABEL_ANCESTOR_DEPTH = 4


def label_resolution_js(*, max_chars: int) -> str:
    """The `labelFor` helper, as JavaScript, for injection into a page.

    `max_chars` differs between the two extractors (120 vs 160) and is the only
    thing that may differ — everything else must stay identical, which is the
    point of generating both from here.
    """
    return f"""
  const labelFor = (el) => {{
    // 1. What HTML declares, in the order the accessibility tree uses.
    if (el.id) {{
      const lab = document.querySelector(`label[for="${{CSS.escape(el.id)}}"]`);
      if (lab) return textOf(lab).slice(0, {max_chars});
    }}
    const wrapped = el.closest('label');
    if (wrapped) return textOf(wrapped).slice(0, {max_chars});
    const aria = el.getAttribute('aria-labelledby');
    if (aria) {{
      const named = aria.split(/\\s+/).map(id => {{
        const n = document.getElementById(id);
        return n ? textOf(n) : '';
      }}).filter(Boolean).join(' ');
      if (named) return named.slice(0, {max_chars});
    }}
    // 2. Nothing declared. Read what a person reads: the nearest caption above
    //    this field. Walk outwards and stop at the FIRST ancestor that has one,
    //    so a field takes its own group's caption rather than a neighbour's.
    //
    //    ONLY for controls that cannot carry their own caption. A button or a
    //    link already says what it is, and letting it borrow a nearby <label>
    //    actively destroys that: with this ungated, `<button>Export Data</button>`
    //    took the name "Role" from an unrelated label elsewhere in its ancestor,
    //    and the permission derived from it disappeared.
    const tag = el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'select' && tag !== 'textarea') return null;
    //    And only for controls a person can actually SEE. This fallback reads
    //    what a reader reads; an invisible field is read by nobody, so giving it
    //    the nearest caption is pure invention. Live: OrangeHRM's login form
    //    carries a hidden `_token` input beside the username group, it was
    //    handed the label "Username", the form then looked like it had two
    //    username fields, and the CSRF token was overwritten — every login in
    //    the run failed with `unresolved_authentication`.
    const box = el.getBoundingClientRect();
    if (el.type === 'hidden' || (box.width === 0 && box.height === 0)) return null;
    let node = el.parentElement;
    for (let depth = 0; node && depth < {MAX_LABEL_ANCESTOR_DEPTH}; depth++) {{
      // <label> first: an application that bothered to use the element meant
      // it as a caption. Only fall back to generic text containers when there
      // is no <label> anywhere in range, since those also hold help text,
      // validation messages and units.
      for (const selector of ['label', 'p, span, div']) {{
        for (const cap of node.querySelectorAll(selector)) {{
          // A container that CONTAINS the field is not its caption — that is
          // the field's own wrapper, and its text includes the field itself.
          if (cap.contains(el)) continue;
          // Another form control inside means this is a different field's
          // group, not a caption for this one.
          if (cap.querySelector('input, select, textarea')) continue;
          const text = textOf(cap);
          if (text) return text.slice(0, {max_chars});
        }}
      }}
      node = node.parentElement;
    }}
    return null;
  }};
"""

"""Raw DOM extraction — the single in-page JavaScript pass the rest of the
Universal Page Perception Engine operates on.

Deterministic, structured, generic: this collects facts (attributes, computed
styles, bounding boxes, DOM nesting) — never raw full-page HTML, and never a
business-specific label. One `page.evaluate()` round-trip produces a
`RawObservation`; every other extractor module works from that shared payload,
so the (comparatively expensive) DOM walk only happens once per observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import Page

from app.application.url_normalize import origin_of, same_origin
from app.browser.label_resolution import label_resolution_js
from app.perception.models import HeadingDescriptor, LinkDescriptor, TextBlockDescriptor
from app.utils.ids import new_id

MAX_ELEMENTS = 400
MAX_TEXT = 2500

# Landmark tags/roles used to seed both per-element `landmark_ancestor` tagging
# and the standalone `regions_raw` list region_classifier consumes.
_LANDMARK_SELECTOR = (
    'header, nav, main, aside, footer, '
    '[role="banner"], [role="navigation"], [role="main"], '
    '[role="complementary"], [role="contentinfo"]'
)

RAW_EXTRACTION_SCRIPT = """
(maxElements) => {
  const pad = (n) => String(n).padStart(3, '0');
  // Seed the counter past however many `data-gemmaqa-id`s already exist on
  // the page — PageObserver's own OBSERVE_SCRIPT (app/browser/observer.py)
  // stamps elements with this SAME attribute using an entirely independent
  // counter that starts at 0 every run. Starting this script's counter at 0
  // too would let it mint a brand-new id (e.g. for an <img> PageObserver
  // never tags) that collides with a number PageObserver already assigned to
  // a completely different element earlier on the same page — verified live:
  // a page with a 4-element nav/tab/dropdown mix plus one image reproduces
  // this every time (the image and the 4th interactive element both get
  // "el_004"). Seeding from the existing count guarantees every newly minted
  // id in THIS pass is higher than anything already claimed, by either
  // script, so far.
  let counter = document.querySelectorAll('[data-gemmaqa-id]').length;
  const nextId = (prefix) => `${prefix}_${pad(++counter)}`;

  // Can clicking this collection ROW (rather than a button inside it) open the
  // record? Two tiers, because the honest answer is often "probably".
  //
  // OBSERVED — a signal actually present in the DOM: an inline handler
  // attribute, an interactive ARIA role, keyboard focusability, a pointer
  // cursor, or a row-spanning link.
  //
  // STRUCTURAL_HYPOTHESIS — none of those are present, but the row carries
  // cells and contains no interactive elements of its own. A framework that
  // attaches its click handler in JavaScript leaves NO DOM trace of it, so
  // "no signal" cannot be read as "not clickable" — verified live on a React
  // record list whose rows navigate on click while exposing no onclick
  // attribute, no role, no tabindex, and no pointer cursor. A row with its own
  // buttons is excluded: there the buttons are the actions, and the row itself
  // usually is not clickable.
  //
  // A hypothesis is safe to hold because acting on it is a read-only click, and
  // wrong hypotheses are self-correcting: no state transition follows, and the
  // candidate is recorded as attempted.
  const rowActivation = (row) => {
    const evidence = [];
    if (row.hasAttribute('onclick')) evidence.push('onclick_attribute');
    const role = (row.getAttribute('role') || '').toLowerCase();
    if (role === 'button' || role === 'link') evidence.push(`role=${role}`);
    if (row.hasAttribute('tabindex') && row.getAttribute('tabindex') !== '-1') {
      evidence.push('focusable_tabindex');
    }
    try {
      if (window.getComputedStyle(row).cursor === 'pointer') evidence.push('cursor_pointer');
    } catch (e) { /* detached node */ }
    if (row.querySelector('a[href]:not([href^="#"])')) evidence.push('contains_navigating_link');

    if (evidence.length > 0) {
      return { activatable: true, evidence: evidence.slice(0, 4), basis: 'observed' };
    }
    const ownControls = row.querySelectorAll('button, a[href], input, select, textarea, [role="button"]');
    const hasCells = row.querySelectorAll('td, [role="gridcell"], [role="cell"]').length > 0
      || textOf(row).length > 0;
    if (ownControls.length === 0 && hasCells) {
      return {
        activatable: true,
        evidence: ['no_row_level_controls', 'record_row_shape'],
        basis: 'structural_hypothesis',
      };
    }
    return { activatable: false, evidence: [], basis: 'none' };
  };

  const textOf = (el) => ((el.innerText || el.textContent || '') + '').trim().replace(/\\s+/g, ' ');

  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  /*__LABEL_FOR__*/

  const describedBy = (el) => {
    const ids = el.getAttribute('aria-describedby');
    if (!ids) return null;
    return ids.split(/\\s+/).map(id => {
      const n = document.getElementById(id);
      return n ? textOf(n) : '';
    }).filter(Boolean).join(' ').slice(0, 240) || null;
  };

  const childImageAlt = (el) => {
    const img = el.querySelector && el.querySelector('img[alt]');
    return img ? (img.getAttribute('alt') || '') : '';
  };

  const accessibleName = (el) => {
    return (
      el.getAttribute('aria-label') ||
      labelFor(el) ||
      el.getAttribute('title') ||
      el.getAttribute('placeholder') ||
      textOf(el) ||
      childImageAlt(el) ||
      el.getAttribute('name') ||
      ''
    ).trim().slice(0, 160);
  };

  const parseBoolAttr = (el, name) => {
    const v = el.getAttribute(name);
    if (v === null) return null;
    return v === 'true' || v === '1' || v === '';
  };

  const parseTabIndex = (el) => {
    const raw = el.getAttribute('tabindex');
    if (raw === null || raw === '') return null;
    const n = parseInt(raw, 10);
    return Number.isNaN(n) ? null : n;
  };

  const categoryOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    // role="tab" must win even on a native <button>/<a> — otherwise the
    // (very common) <button role="tab"> pattern always falls through to the
    // generic "button" category below and is never recognized as a tab.
    if (role === 'tab') return 'tab';
    if (tag === 'a' || role === 'link') return 'link';
    if (tag === 'button' || role === 'button' || type === 'button' || type === 'submit' || type === 'reset') return 'button';
    if (tag === 'select' || role === 'combobox' || role === 'listbox') return 'select';
    if (type === 'checkbox' || role === 'checkbox') return 'checkbox';
    if (type === 'radio' || role === 'radio') return 'radio';
    if (type === 'file') return 'file';
    if (role === 'tab') return 'tab';
    if (tag === 'textarea') return 'textarea';
    if (tag === 'input') return 'input';
    return 'other';
  };

  const NATIVE_FOCUSABLE = new Set(['a', 'button', 'input', 'select', 'textarea', 'summary', 'audio', 'video']);

  const landmarkAncestorOf = (el) => {
    const found = el.closest && el.closest('header, nav, main, aside, footer, [role="banner"], [role="navigation"], [role="main"], [role="complementary"], [role="contentinfo"]');
    if (!found) return null;
    return found.getAttribute('data-gemmaqa-region-id') || null;
  };

  const containerRoleOf = (el) => {
    const found = el.closest && el.closest(
      '[role="dialog"], [role="alertdialog"], dialog, .modal, table, ul, ol, [role="list"], [role="listbox"], [role="tablist"]'
    );
    if (!found) return null;
    const tag = found.tagName.toLowerCase();
    const role = (found.getAttribute('role') || '').toLowerCase();
    return role || tag;
  };

  // --- Landmark regions (seeds region_classifier) ---------------------------
  const regions_raw = [];
  document.querySelectorAll('header, nav, main, aside, footer, [role="banner"], [role="navigation"], [role="main"], [role="complementary"], [role="contentinfo"]').forEach((el) => {
    if (regions_raw.length >= 40) return;
    if (!isVisible(el)) return;
    const regionId = nextId('region');
    el.setAttribute('data-gemmaqa-region-id', regionId);
    const rect = el.getBoundingClientRect();
    regions_raw.push({
      dom_id: regionId,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role'),
      aria_label: el.getAttribute('aria-label'),
      text_snippet: textOf(el).slice(0, 200),
      bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
    });
  });

  // --- Interactive-ish + structural elements ---------------------------------
  const interactiveSelector = [
    'a', 'button', 'input', 'select', 'textarea', 'summary',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[role="checkbox"]', '[role="radio"]', '[role="switch"]', '[role="combobox"]',
    '[role="option"]', '[contenteditable="true"]', '[onclick]', '[tabindex]',
    '[aria-expanded]', '[aria-haspopup]',
  ].join(',');

  const buildElementRecord = (el, id) => {
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type');
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const options = tag === 'select'
      ? Array.from(el.options || []).map(o => (o.text || o.value || '').trim()).filter(Boolean).slice(0, 15)
      : [];

    let currentValue = null;
    try {
      if (tag === 'input' || tag === 'textarea' || tag === 'select') {
        const t = (type || '').toLowerCase();
        if (t === 'password') currentValue = '***';
        else currentValue = String(el.value || '').slice(0, 120);
      }
    } catch (e) {}

    const looksIconOnly = !textOf(el) && (
      !!(el.querySelector && el.querySelector('svg, img')) ||
      /\\bicon\\b/i.test(el.className || '')
    );
    const hasInlineSvg = !!(el.querySelector && el.querySelector('svg'));

    return {
      dom_id: id,
      tag,
      role: el.getAttribute('role'),
      type,
      name: el.getAttribute('name'),
      id_attr: el.id || null,
      test_id: el.getAttribute('data-testid'),
      text: textOf(el).slice(0, 160),
      aria_label: el.getAttribute('aria-label'),
      accessible_name: accessibleName(el),
      accessible_description: describedBy(el),
      label: labelFor(el),
      href: el.getAttribute('href'),
      placeholder: el.getAttribute('placeholder'),
      title_attr: el.getAttribute('title'),
      is_visible: isVisible(el),
      is_enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      checked: (type === 'checkbox' || type === 'radio') ? !!el.checked : parseBoolAttr(el, 'aria-checked'),
      aria_expanded: parseBoolAttr(el, 'aria-expanded'),
      aria_selected: parseBoolAttr(el, 'aria-selected'),
      aria_current: el.getAttribute('aria-current'),
      aria_haspopup: el.getAttribute('aria-haspopup'),
      tabindex: parseTabIndex(el),
      is_native_focusable: NATIVE_FOCUSABLE.has(tag),
      cursor_style: style.cursor || null,
      has_onclick_attr: el.hasAttribute('onclick') || typeof el.onclick === 'function',
      looks_icon_only: looksIconOnly,
      has_inline_svg: hasInlineSvg,
      current_value: currentValue,
      available_options: options,
      category: categoryOf(el),
      bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      landmark_region_id: landmarkAncestorOf(el),
      container_role: containerRoleOf(el),
      selector_hint: `[data-gemmaqa-id="${id}"]`,
      // --- Field-semantics signals (autocomplete/multi-select/date/radio-
      // group/checkbox-group support) — additive, never replaces the
      // existing label/accessible_name chain above.
      multiple: !!el.multiple,
      list_attr: el.getAttribute('list'),
      aria_autocomplete: el.getAttribute('aria-autocomplete'),
      aria_multiselectable: parseBoolAttr(el, 'aria-multiselectable'),
      input_mode: el.getAttribute('inputmode'),
      // Position signal for floating-action-button detection — a FAB is
      // structurally "icon-only + fixed/absolute positioned", never a
      // business-word guess.
      position_style: style.position || null,
    };
  };

  const elements = [];
  const seen = new Set();
  document.querySelectorAll(interactiveSelector).forEach((el) => {
    if (seen.has(el)) return;
    seen.add(el);
    if (elements.length >= maxElements) return;

    let id = el.getAttribute('data-gemmaqa-id');
    if (!id) {
      id = nextId('el');
      el.setAttribute('data-gemmaqa-id', id);
    }
    elements.push(buildElementRecord(el, id));
  });

  // --- Generic non-semantic clickables (the audit's key gap) -----------------
  // A <div>/<span>/<li>/<article> made clickable purely via a framework's
  // synthetic event system (React onClick, etc.) carries NONE of the above
  // selector's signals — no role, no tabindex, no onclick attribute — the only
  // externally observable trace is a `cursor: pointer` computed style. Swept
  // separately (capped, and only for elements with a real footprint) so this
  // doesn't turn into a full-page style scan.
  let pointerSweepCount = 0;
  document.querySelectorAll('div, span, li, article, section').forEach((el) => {
    if (seen.has(el)) return;
    if (pointerSweepCount >= 60 || elements.length >= maxElements) return;
    if (!isVisible(el)) return;
    const rect = el.getBoundingClientRect();
    if (rect.width < 24 || rect.height < 24) return;
    const cursor = window.getComputedStyle(el).cursor;
    if (cursor !== 'pointer') return;
    seen.add(el);
    pointerSweepCount += 1;
    let id = el.getAttribute('data-gemmaqa-id');
    if (!id) {
      id = nextId('el');
      el.setAttribute('data-gemmaqa-id', id);
    }
    elements.push(buildElementRecord(el, id));
  });

  // --- Headings (h1-h6 + role=heading, level preserved) ----------------------
  const headings = [];
  document.querySelectorAll('h1, h2, h3, h4, h5, h6, [role="heading"]').forEach((h) => {
    if (headings.length >= 40) return;
    const text = textOf(h);
    if (!text) return;
    let level = 1;
    const tagMatch = /^h([1-6])$/.exec(h.tagName.toLowerCase());
    if (tagMatch) level = parseInt(tagMatch[1], 10);
    else {
      const ariaLevel = h.getAttribute('aria-level');
      if (ariaLevel) level = Math.min(6, Math.max(1, parseInt(ariaLevel, 10) || 1));
    }
    let hid = h.getAttribute('data-gemmaqa-id');
    if (!hid) { hid = nextId('el'); h.setAttribute('data-gemmaqa-id', hid); }
    headings.push({ dom_id: hid, text: text.slice(0, 200), level });
  });

  // --- Forms (full field list, no field cap beyond a sane ceiling) ----------
  const forms = [];
  document.querySelectorAll('form').forEach((form) => {
    if (forms.length >= 15) return;
    let formId = form.getAttribute('data-gemmaqa-id');
    if (!formId) { formId = nextId('form'); form.setAttribute('data-gemmaqa-id', formId); }
    const rect = form.getBoundingClientRect();
    const fields = [];
    let submitId = null;
    // Native controls PLUS controls a framework builds out of divs and declares
    // with ARIA. Collecting only input/select/textarea/button meant a form's
    // custom pickers were absent from the model entirely: OrangeHRM's Add User
    // form reached the agent as Employee Name + Username + Password + Confirm
    // Password — the exact shape of a sign-up form — because its User Role and
    // Status pickers are <div class="oxd-select-wrapper"> with a text node. The
    // agent concluded it was looking at a registration page, filled in the login
    // credential, and looped on Save for 170 actions.
    //
    // ARIA roles are a web standard, so this recognises the pattern rather than
    // any one framework — the same principle as rowActivation above, which had
    // to see click handlers React attaches in JavaScript.
    const CUSTOM_CONTROL_SELECTOR = [
      '[role="combobox"]', '[role="listbox"]', '[role="radiogroup"]',
      '[role="switch"]', '[role="spinbutton"]', '[role="slider"]',
      '[role="searchbox"]', '[role="textbox"]',
      '[aria-haspopup="listbox"]', '[contenteditable="true"]',
      // Confirmed against OrangeHRM: its pickers carry NO ARIA at all — they are
      // <div class="oxd-select-text-input" tabindex="0">. `tabindex` on a
      // non-native element inside a form is the standard way to say "the user
      // operates this", so it finds the control without naming any framework.
      // Scoped to a form and excluding native tags, this matched exactly the two
      // pickers on that page and nothing else.
      '[tabindex]:not(input):not(select):not(textarea):not(button):not(a)',
    ].join(', ');
    form.querySelectorAll('input, select, textarea, button, ' + CUSTOM_CONTROL_SELECTOR).forEach((field) => {
      let fieldId = field.getAttribute('data-gemmaqa-id');
      if (!fieldId) { fieldId = nextId('el'); field.setAttribute('data-gemmaqa-id', fieldId); }
      const tag = field.tagName.toLowerCase();
      const explicitRole = (field.getAttribute('role') || '').toLowerCase();
      const isNative = ['input', 'select', 'textarea', 'button'].includes(tag);
      const type = (field.getAttribute('type') || tag).toLowerCase();
      const isSubmitLike = type === 'submit' || type === 'button' || type === 'reset' || tag === 'button';
      if (isSubmitLike && !submitId) submitId = fieldId;

      // A custom control: not a native form element, but ARIA says it takes
      // input. Reported with the field_type its role implies, so downstream code
      // that already understands "select" or "combobox" needs no special case.
      if (!isNative) {
        // A native control nested inside the custom widget already represents it
        // (a combobox wrapping a real <input> is one field, not two).
        if (field.querySelector('input, select, textarea')) return;
        const roleTypes = {
          combobox: 'select', listbox: 'select', radiogroup: 'radio',
          switch: 'checkbox', spinbutton: 'number', slider: 'range',
          searchbox: 'text', textbox: 'text',
        };
        const customType = roleTypes[explicitRole]
          || (field.getAttribute('aria-haspopup') === 'listbox' ? 'select' : 'text');
        fields.push({
          dom_id: fieldId,
          name: field.getAttribute('name'),
          field_type: customType,
          label: labelFor(field),
          accessible_name: accessibleName(field),
          accessible_description: describedBy(field),
          required: field.getAttribute('aria-required') === 'true',
          placeholder: field.getAttribute('placeholder'),
          options: [],
          disabled: field.getAttribute('aria-disabled') === 'true',
          checked: explicitRole === 'switch'
            ? field.getAttribute('aria-checked') === 'true'
            : null,
          // Whatever the widget currently displays IS its value to a tester;
          // there is no `.value` to read on a div.
          current_value: (field.textContent || '').trim().slice(0, 80),
          role: explicitRole || null,
          multiple: field.getAttribute('aria-multiselectable') === 'true',
          list_attr: null,
          aria_autocomplete: field.getAttribute('aria-autocomplete'),
          minlength: null,
          maxlength: null,
          pattern: null,
          min_attr: field.getAttribute('aria-valuemin'),
          max_attr: field.getAttribute('aria-valuemax'),
          step_attr: null,
          custom_control: true,
        });
        return;
      }

      if (['input', 'select', 'textarea'].includes(tag) && !isSubmitLike) {
        const options = tag === 'select'
          ? Array.from(field.options || []).map(o => (o.text || '').trim()).slice(0, 15)
          : [];
        let value = '';
        try { value = type === 'password' ? '***' : String(field.value || '').slice(0, 80); } catch (e) {}
        fields.push({
          dom_id: fieldId,
          name: field.getAttribute('name'),
          field_type: type,
          label: labelFor(field),
          accessible_name: accessibleName(field),
          accessible_description: describedBy(field),
          required: !!field.required || field.getAttribute('aria-required') === 'true',
          placeholder: field.getAttribute('placeholder'),
          options,
          disabled: !!field.disabled,
          checked: (type === 'checkbox' || type === 'radio') ? !!field.checked : null,
          current_value: value,
          // Field-semantics signals (combobox/autocomplete/multi-select/
          // date-picker support) — see app.perception.form_extractor.
          role: field.getAttribute('role'),
          multiple: !!field.multiple,
          list_attr: field.getAttribute('list'),
          aria_autocomplete: field.getAttribute('aria-autocomplete'),
          // Constraint-inference signals — see app.agent.field_constraint_inference.
          minlength: field.getAttribute('minlength'),
          maxlength: field.getAttribute('maxlength'),
          pattern: field.getAttribute('pattern'),
          min_attr: field.getAttribute('min'),
          max_attr: field.getAttribute('max'),
          step_attr: field.getAttribute('step'),
        });
      }
    });
    forms.push({
      dom_id: formId,
      action: form.getAttribute('action'),
      method: form.getAttribute('method'),
      fields,
      submit_element_id: submitId,
      bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
    });
  });

  // --- Tables (full rows, per-row action-button linkage) --------------------
  const tables = [];
  document.querySelectorAll('table').forEach((table) => {
    if (tables.length >= 10) return;
    let tableId = table.getAttribute('data-gemmaqa-id');
    if (!tableId) { tableId = nextId('table'); table.setAttribute('data-gemmaqa-id', tableId); }
    const rect = table.getBoundingClientRect();
    const headerCells = Array.from(table.querySelectorAll('thead th, tr:first-child th'));
    const headers = headerCells.map(th => textOf(th)).filter(Boolean).slice(0, 30);
    const bodyRows = Array.from(table.querySelectorAll('tbody tr'));
    const rowsSource = bodyRows.length ? bodyRows : Array.from(table.querySelectorAll('tr')).slice(headers.length ? 1 : 0);
    const rows = rowsSource.slice(0, 60).map((row, idx) => {
      const cells = Array.from(row.querySelectorAll('td')).map(td => textOf(td).slice(0, 120));
      const actionIds = [];
      row.querySelectorAll('button, a, [role="button"]').forEach((actionEl) => {
        let actionId = actionEl.getAttribute('data-gemmaqa-id');
        if (!actionId) { actionId = nextId('el'); actionEl.setAttribute('data-gemmaqa-id', actionId); }
        actionIds.push(actionId);
      });
      // Native table rows used to get no id at all, unlike the ARIA-grid and
      // div-group paths below. Without one, a clickable row cannot be acted on:
      // observed live, where a record list rendered a row per record and the
      // agent could not open ANY of them, so update and delete were
      // permanently unreachable.
      let rowId = row.getAttribute('data-gemmaqa-id');
      if (!rowId) { rowId = nextId('el'); row.setAttribute('data-gemmaqa-id', rowId); }
      const act = rowActivation(row);
      return {
        dom_id: rowId, row_index: idx, cell_values: cells, row_action_ids: actionIds,
        activatable: act.activatable, activation_evidence: act.evidence, activation_basis: act.basis,
      };
    });
    tables.push({
      dom_id: tableId,
      headers,
      row_count: rowsSource.length,
      rows,
      bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
    });
  });

  // --- Universal record collections (framework-neutral) ---------------------
  // Detects "this page shows a set of records" from STRUCTURE alone: ARIA
  // grid/treegrid roles, or a repeated-sibling shape (div-based rows/cards).
  // Never keyed off a framework name — React/Vue/Angular all produce the
  // same observable DOM structure this looks for.
  const collections = [];

  document.querySelectorAll('[role="grid"], [role="treegrid"], [role="table"]').forEach((grid) => {
    if (collections.length >= 12) return;
    if (!isVisible(grid)) return;
    const rowEls = Array.from(grid.querySelectorAll('[role="row"]'));
    if (rowEls.length < 1) return;
    let gridId = grid.getAttribute('data-gemmaqa-id');
    if (!gridId) { gridId = nextId('collection'); grid.setAttribute('data-gemmaqa-id', gridId); }
    const firstRowHeaderCells = rowEls[0] ? Array.from(rowEls[0].querySelectorAll('[role="columnheader"]')) : [];
    const headers = firstRowHeaderCells.map((c) => textOf(c)).filter(Boolean);
    const dataRowEls = headers.length ? rowEls.slice(1) : rowEls;
    const rows = dataRowEls.slice(0, 60).map((row, idx) => {
      const cells = Array.from(row.querySelectorAll('[role="gridcell"], [role="cell"]')).map((c) => textOf(c).slice(0, 120));
      const actionIds = [];
      row.querySelectorAll('button, a, [role="button"]').forEach((actionEl) => {
        let actionId = actionEl.getAttribute('data-gemmaqa-id');
        if (!actionId) { actionId = nextId('el'); actionEl.setAttribute('data-gemmaqa-id', actionId); }
        actionIds.push(actionId);
      });
      let rowId = row.getAttribute('data-gemmaqa-id');
      if (!rowId) { rowId = nextId('el'); row.setAttribute('data-gemmaqa-id', rowId); }
      const act = rowActivation(row);
      return {
        dom_id: rowId, row_index: idx, cell_values: cells, row_action_ids: actionIds,
        activatable: act.activatable, activation_evidence: act.evidence, activation_basis: act.basis,
      };
    });
    const rect = grid.getBoundingClientRect();
    collections.push({
      dom_id: gridId,
      collection_type: grid.getAttribute('role') === 'treegrid' ? 'aria_treegrid' : 'aria_grid',
      headers,
      row_count: dataRowEls.length,
      rows,
      bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      structural_signals: ['aria_grid_role'],
    });
  });

  const _claimedCollectionContainers = new Set();
  document.querySelectorAll('table, [role="grid"], [role="treegrid"], [role="table"], nav, [role="navigation"], ul, ol, [role="list"]')
    .forEach((c) => _claimedCollectionContainers.add(c));

  const _classSignature = (el) => {
    const cls = (el.className || '').toString().trim().split(/\\s+/).filter(Boolean).sort().join('.');
    return `${el.tagName.toLowerCase()}#${cls}`;
  };

  let _divGroupCount = 0;
  document.querySelectorAll('div, section, article, ul, ol').forEach((container) => {
    if (_divGroupCount >= 8 || collections.length >= 12) return;
    if (_claimedCollectionContainers.has(container)) return;
    if (container.closest && container.closest('table, [role="grid"], [role="treegrid"], nav, [role="navigation"]')) return;
    const children = Array.from(container.children).filter(isVisible);
    if (children.length < 3) return;
    const sigCounts = {};
    children.forEach((c) => { const s = _classSignature(c); sigCounts[s] = (sigCounts[s] || 0) + 1; });
    const bestSig = Object.entries(sigCounts).sort((a, b) => b[1] - a[1])[0];
    if (!bestSig || bestSig[1] < 3) return;
    const repeated = children.filter((c) => _classSignature(c) === bestSig[0]);
    if (repeated.length < 3) return;

    const boxes = repeated.map((c) => c.getBoundingClientRect());
    const sortedByY = [...boxes].sort((a, b) => a.y - b.y);
    const isStacked = sortedByY.every((b, i) => i === 0 || b.y >= sortedByY[i - 1].y - 2);
    const avgHeight = boxes.reduce((s, b) => s + b.height, 0) / boxes.length;
    const looksLikeCard = avgHeight >= 80 && repeated.some((c) => c.querySelector('button, a, [role="button"]'));
    if (!isStacked && !looksLikeCard) return;

    let containerId = container.getAttribute('data-gemmaqa-id');
    if (!containerId) { containerId = nextId('collection'); container.setAttribute('data-gemmaqa-id', containerId); }

    let headers = [];
    const prevSibling = repeated[0].previousElementSibling;
    if (prevSibling && !repeated.includes(prevSibling)) {
      const headerCells = Array.from(prevSibling.children).map((c) => textOf(c)).filter(Boolean);
      if (headerCells.length >= 2 && headerCells.length <= 12) headers = headerCells;
    }

    const rows = repeated.slice(0, 60).map((row, idx) => {
      const textNodes = Array.from(row.querySelectorAll('*'))
        .filter((n) => n.children.length === 0)
        .map((n) => textOf(n))
        .filter(Boolean);
      const actionIds = [];
      row.querySelectorAll('button, a, [role="button"]').forEach((actionEl) => {
        let actionId = actionEl.getAttribute('data-gemmaqa-id');
        if (!actionId) { actionId = nextId('el'); actionEl.setAttribute('data-gemmaqa-id', actionId); }
        actionIds.push(actionId);
      });
      let rowId = row.getAttribute('data-gemmaqa-id');
      if (!rowId) { rowId = nextId('el'); row.setAttribute('data-gemmaqa-id', rowId); }
      const act = rowActivation(row);
      return {
        dom_id: rowId, row_index: idx, cell_values: textNodes.slice(0, 20), row_action_ids: actionIds,
        activatable: act.activatable, activation_evidence: act.evidence, activation_basis: act.basis,
      };
    });

    const rect = container.getBoundingClientRect();
    _divGroupCount += 1;
    collections.push({
      dom_id: containerId,
      collection_type: looksLikeCard && !isStacked ? 'card_group' : 'div_row_group',
      headers,
      row_count: repeated.length,
      rows,
      bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      structural_signals: [
        'repeated_class_signature',
        isStacked ? 'vertical_stack_layout' : 'card_wrap_layout',
      ],
    });
  });

  // --- Lists ------------------------------------------------------------------
  const lists = [];
  document.querySelectorAll('ul, ol, [role="list"]').forEach((listEl) => {
    if (lists.length >= 20) return;
    if (!isVisible(listEl)) return;
    const items = Array.from(listEl.children).filter(li =>
      li.tagName.toLowerCase() === 'li' || (li.getAttribute('role') || '') === 'listitem'
    );
    if (items.length < 2) return; // skip trivial/non-content lists (e.g. single-item menus)
    let listId = listEl.getAttribute('data-gemmaqa-id');
    if (!listId) { listId = nextId('el'); listEl.setAttribute('data-gemmaqa-id', listId); }
    lists.push({
      dom_id: listId,
      item_count: items.length,
      sample_items: items.slice(0, 5).map(li => textOf(li).slice(0, 120)).filter(Boolean),
    });
  });

  // --- Images -------------------------------------------------------------
  const images = [];
  document.querySelectorAll('img').forEach((img) => {
    if (images.length >= 60) return;
    if (!isVisible(img)) return;
    let imgId = img.getAttribute('data-gemmaqa-id');
    if (!imgId) { imgId = nextId('el'); img.setAttribute('data-gemmaqa-id', imgId); }
    const parent = img.parentElement;
    const surrounding = parent ? textOf(parent).slice(0, 160) : '';
    const rect = img.getBoundingClientRect();
    images.push({
      dom_id: imgId,
      alt: img.getAttribute('alt'),
      role: img.getAttribute('role'),
      src: img.getAttribute('src'),
      class_name: (img.className || '').toString(),
      surrounding_text: surrounding,
      in_interactive_container: !!(img.closest && img.closest('a, button, [role="button"], [role="link"]')),
      bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
    });
  });

  // --- Canvas elements (opaque pixel content — never programmatically
  // classifiable; a direct trigger for the visual observation policy) --------
  const canvases = [];
  document.querySelectorAll('canvas').forEach((c) => {
    if (canvases.length >= 20) return;
    if (!isVisible(c)) return;
    let cId = c.getAttribute('data-gemmaqa-id');
    if (!cId) { cId = nextId('el'); c.setAttribute('data-gemmaqa-id', cId); }
    const rect = c.getBoundingClientRect();
    canvases.push({
      dom_id: cId,
      aria_label: c.getAttribute('aria-label'),
      role: c.getAttribute('role'),
      surrounding_text: c.parentElement ? textOf(c.parentElement).slice(0, 160) : '',
      bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
    });
  });

  // --- Dialogs / modals (+ contained element ids) ----------------------------
  const dialogs = [];
  document.querySelectorAll('dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"], .modal.show, .modal[open], [class*="Modal"][aria-modal="true"]')
    .forEach((d) => {
      if (dialogs.length >= 10) return;
      if (!isVisible(d)) return;
      let dId = d.getAttribute('data-gemmaqa-id');
      if (!dId) { dId = nextId('el'); d.setAttribute('data-gemmaqa-id', dId); }
      const containedIds = [];
      d.querySelectorAll('[data-gemmaqa-id]').forEach((child) => {
        const cid = child.getAttribute('data-gemmaqa-id');
        if (cid && cid !== dId) containedIds.push(cid);
      });
      dialogs.push({
        dom_id: dId,
        role: d.getAttribute('role'),
        text: textOf(d).slice(0, 200),
        contained_element_ids: containedIds.slice(0, 40),
        detected_by: 'aria',
      });
    });

  // An overlay that declares no ARIA role is still an overlay. OrangeHRM's photo
  // viewer is a plain `<div class="orangehrm-photo-carousel --web">`: it matches
  // none of the selectors above, so no `close_dialog` candidate was ever offered,
  // so it stayed open, and every click underneath it failed. Measured in run
  // a1f300a5 action 40 — a 15s timeout on the Buzz submit button and the create
  // abandoned, with the overlay opened fifteen actions earlier.
  //
  // Detect it by MECHANISM rather than vocabulary. Painting content over
  // unrelated content requires a positioned stacking layer; covering the page
  // requires area; sitting over the content requires holding the viewport
  // centre. All three are measurable, and together they exclude the things that
  // are positioned but block nothing: a sticky top bar (5% of this viewport), a
  // nav sidebar (16%, and off-centre), a corner toast, a bottom cookie bar.
  //
  // Reported ONLY into the canonical model, which is what drives `close_dialog`.
  // `PageState.dialogs` is what the model is TOLD about the page, and a nav
  // sidebar described there as a dialog would manufacture bug reports.
  const OVERLAY_MIN_VIEWPORT_RATIO = 0.25;
  const centre = document.elementFromPoint(
    Math.floor(innerWidth / 2), Math.floor(innerHeight / 2));
  if (centre) {
    const viewportArea = Math.max(1, innerWidth * innerHeight);
    const seen = new Set(dialogs.map((d) => d.dom_id));
    for (let n = centre; n && dialogs.length < 12; n = n.parentElement) {
      const s = getComputedStyle(n);
      const positioned = s.position === 'fixed'
        || (s.position === 'absolute' && s.zIndex !== 'auto');
      if (!positioned || !isVisible(n)) continue;
      const r = n.getBoundingClientRect();
      if ((r.width * r.height) / viewportArea < OVERLAY_MIN_VIEWPORT_RATIO) continue;
      let oId = n.getAttribute('data-gemmaqa-id');
      if (!oId) { oId = nextId('el'); n.setAttribute('data-gemmaqa-id', oId); }
      if (seen.has(oId)) continue;
      seen.add(oId);
      const contained = [];
      n.querySelectorAll('[data-gemmaqa-id]').forEach((child) => {
        const cid = child.getAttribute('data-gemmaqa-id');
        if (cid && cid !== oId) contained.push(cid);
      });
      dialogs.push({
        dom_id: oId,
        role: n.getAttribute('role'),
        text: textOf(n).slice(0, 200),
        contained_element_ids: contained.slice(0, 40),
        detected_by: 'stacking_layer',
      });
    }
  }

  // --- Alerts / toasts (severity heuristic from role/class, never content) --
  const severityOf = (el) => {
    const role = (el.getAttribute('role') || '').toLowerCase();
    const cls = (el.className || '').toString().toLowerCase();
    if (role === 'alert' || /\\berror\\b|\\bdanger\\b/.test(cls)) return 'error';
    if (/\\bwarn/.test(cls)) return 'warning';
    if (/\\bsuccess\\b/.test(cls)) return 'success';
    return 'info';
  };
  const alerts = [];
  document.querySelectorAll('[role="alert"], [role="status"], .alert, .Alert, .toast, .Toast, [class*="toast"], [class*="snackbar"]')
    .forEach((a) => {
      if (alerts.length >= 15) return;
      if (!isVisible(a)) return;
      const text = textOf(a);
      if (!text) return;
      alerts.push({ text: text.slice(0, 200), severity: severityOf(a) });
    });

  // --- Navigation containers (grouped, with href per item) -------------------
  const navigation_containers = [];
  document.querySelectorAll('nav, [role="navigation"]').forEach((navEl) => {
    if (navigation_containers.length >= 10) return;
    if (!isVisible(navEl)) return;
    const items = Array.from(navEl.querySelectorAll('a, [role="link"]'))
      .map((a) => {
        let itemId = a.getAttribute('data-gemmaqa-id');
        if (!itemId) { itemId = nextId('el'); a.setAttribute('data-gemmaqa-id', itemId); }
        return {
          dom_id: itemId,
          text: textOf(a) || a.getAttribute('aria-label') || '',
          href: a.getAttribute('href'),
          is_current: a.getAttribute('aria-current'),
        };
      })
      .filter(i => i.text)
      .slice(0, 40);
    if (!items.length) return;
    navigation_containers.push({
      role: navEl.getAttribute('role') || 'nav',
      aria_label: navEl.getAttribute('aria-label'),
      items,
    });
  });

  // --- Breadcrumbs (with href) ------------------------------------------------
  const breadcrumbs = [];
  document.querySelectorAll('nav[aria-label*="breadcrumb" i] li, .breadcrumb li, [class*="breadcrumb"] li')
    .forEach((li) => {
      if (breadcrumbs.length >= 12) return;
      const text = textOf(li);
      if (!text) return;
      const link = li.querySelector('a');
      breadcrumbs.push({ text: text.slice(0, 120), href: link ? link.getAttribute('href') : null });
    });

  // --- Pagination (+ numeric current/total heuristic) ------------------------
  const paginationEls = Array.from(
    document.querySelectorAll('[aria-label*="pagination" i] a, [aria-label*="pagination" i] button, .pagination a, .pagination button, nav[aria-label*="Page" i] a')
  ).filter(isVisible);
  let pagination = null;
  if (paginationEls.length) {
    const items = paginationEls.slice(0, 20).map(el => ({
      text: textOf(el) || el.getAttribute('aria-label') || '',
      is_current: el.getAttribute('aria-current') === 'page' || /\\bactive\\b|\\bcurrent\\b/.test((el.className || '').toString()),
    })).filter(i => i.text);
    const numeric = items.map(i => parseInt(i.text, 10)).filter(n => !Number.isNaN(n));
    const currentItem = items.find(i => i.is_current);
    const currentNumeric = currentItem ? parseInt(currentItem.text, 10) : NaN;
    pagination = {
      items,
      current_page: Number.isNaN(currentNumeric) ? null : currentNumeric,
      total_pages: numeric.length ? Math.max(...numeric) : null,
    };
  }

  const bodyText = (document.body && document.body.innerText) ? document.body.innerText : '';

  return {
    url: location.href,
    title: document.title || '',
    visible_text: bodyText.slice(0, 2500),
    elements,
    headings,
    forms,
    tables,
    collections,
    lists,
    images,
    canvases,
    dialogs,
    alerts,
    navigation_containers,
    breadcrumbs,
    pagination,
    regions_raw,
  };
}
""".replace("/*__LABEL_FOR__*/", label_resolution_js(max_chars=160))


@dataclass
class RawObservation:
    """Everything the single JS pass collected, unprocessed. Every other
    extractor module reads from this — the DOM is only walked once."""

    url: str = ""
    title: str = ""
    visible_text: str = ""
    elements: list[dict[str, Any]] = field(default_factory=list)
    headings: list[dict[str, Any]] = field(default_factory=list)
    forms: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    collections: list[dict[str, Any]] = field(default_factory=list)
    lists: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    canvases: list[dict[str, Any]] = field(default_factory=list)
    dialogs: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    navigation_containers: list[dict[str, Any]] = field(default_factory=list)
    breadcrumbs: list[dict[str, Any]] = field(default_factory=list)
    pagination: dict[str, Any] | None = None
    regions_raw: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_raw_dict(cls, raw: dict[str, Any]) -> "RawObservation":
        return cls(
            url=str(raw.get("url") or ""),
            title=str(raw.get("title") or ""),
            visible_text=str(raw.get("visible_text") or "")[:MAX_TEXT],
            elements=list(raw.get("elements") or [])[:MAX_ELEMENTS],
            headings=list(raw.get("headings") or []),
            forms=list(raw.get("forms") or []),
            tables=list(raw.get("tables") or []),
            collections=list(raw.get("collections") or []),
            lists=list(raw.get("lists") or []),
            images=list(raw.get("images") or []),
            canvases=list(raw.get("canvases") or []),
            dialogs=list(raw.get("dialogs") or []),
            alerts=list(raw.get("alerts") or []),
            navigation_containers=list(raw.get("navigation_containers") or []),
            breadcrumbs=list(raw.get("breadcrumbs") or []),
            pagination=raw.get("pagination"),
            regions_raw=list(raw.get("regions_raw") or []),
        )


async def extract_raw(page: Page, *, max_elements: int = MAX_ELEMENTS) -> RawObservation:
    """Run the single JS extraction pass against a live Playwright page. This is
    the ONLY place in the Perception Engine that touches the browser — every
    other extractor module is pure Python over the resulting `RawObservation`."""
    try:
        raw = await page.evaluate(RAW_EXTRACTION_SCRIPT, max_elements)
    except Exception as exc:
        if "Execution context was destroyed" not in str(exc):
            raise
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        raw = await page.evaluate(RAW_EXTRACTION_SCRIPT, max_elements)
    if not isinstance(raw, dict):
        raw = {}
    return RawObservation.from_raw_dict(raw)


# ---------------------------------------------------------------------------
# Flat DOM facts — headings, visible text, links (images/forms/tables/dialogs/
# navigation each have their own dedicated extractor module).
# ---------------------------------------------------------------------------


def extract_headings(raw: RawObservation) -> list[HeadingDescriptor]:
    return [
        HeadingDescriptor(
            element_id=h.get("dom_id"),
            stable_id=h.get("dom_id") or new_id(),
            text=h.get("text"),
            level=int(h.get("level") or 1),
            dom_tag=f"h{h.get('level') or 1}",
            source="dom",
        )
        for h in raw.headings
        if h.get("text")
    ] or []


def extract_text_blocks(raw: RawObservation) -> list[TextBlockDescriptor]:
    if not raw.visible_text:
        return []
    return [TextBlockDescriptor(text=raw.visible_text, block_type="summary", source="dom")]


def extract_links(raw: RawObservation) -> list[LinkDescriptor]:
    links: list[LinkDescriptor] = []
    for el in raw.elements:
        category = el.get("category")
        tag = (el.get("tag") or "").lower()
        if category != "link" and tag != "a":
            continue
        href = el.get("href")
        is_external = None
        if href and raw.url:
            # Resolve relative hrefs ("/cart") against the page URL first —
            # same_origin()/origin_of() only understand absolute URLs, so an
            # unresolved relative href would otherwise always compare as a
            # (wrongly) different, "external" origin.
            is_external = not same_origin(urljoin(raw.url, href), origin_of(raw.url))
        links.append(
            LinkDescriptor(
                element_id=el.get("dom_id"),
                stable_id=el.get("dom_id") or new_id(),
                text=el.get("text"),
                accessible_name=el.get("accessible_name"),
                dom_tag=el.get("tag"),
                aria_role=el.get("role"),
                url=href,
                target_url=href,
                is_external=is_external,
                is_external_url=is_external,
                is_visible=bool(el.get("is_visible", True)),
                is_enabled=bool(el.get("is_enabled", True)),
                bounding_box=el.get("bounding_box"),
                parent_region_id=el.get("landmark_region_id"),
                source="dom",
            )
        )
    return links

"""Extract structured PageState from the live browser (no raw HTML dumps)."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from app.browser.console_monitor import ConsoleMonitor
from app.browser.fingerprint import fingerprint_page_state
from app.browser.label_resolution import label_resolution_js
from app.browser.locators import LocatorRegistry, choose_locator_strategy
from app.browser.network_monitor import NetworkMonitor
from app.config import get_settings
from app.schemas import (
    FormDescriptor,
    FormField,
    InteractiveElement,
    PageState,
    TableDescriptor,
)
from app.utils.ids import new_id
from app.utils.logging import get_logger
from app.utils.sanitization import truncate_text

logger = get_logger("browser.observer")

# Caps to keep Gemma prompts small
MAX_ELEMENTS = 100
MAX_HEADINGS = 20
MAX_TABLES = 8
MAX_FORMS = 10
MAX_OPTIONS = 15
MAX_TEXT = 800

OBSERVE_SCRIPT = """
() => {
  const pad = (n) => String(n).padStart(3, '0');
  let counter = 0;
  const nextId = (prefix) => `${prefix}_${pad(++counter)}`;

  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const textOf = (el) => ((el.innerText || el.textContent || '') + '').trim().replace(/\\s+/g, ' ');

  /*__LABEL_FOR__*/

  const childImageAlt = (el) => {
    // Standard accessible-name computation: an image-only link/button (no direct
    // text) takes its name from a wrapped <img alt="...">. Without this, a product
    // image link and its sibling title link (e.g. SauceDemo's inventory cards) look
    // like two completely unrelated, unnamed controls instead of two routes to the
    // same destination — so exploration wastes turns on the "same" product twice
    // before ever reaching a different one.
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
    ).trim().slice(0, 120);
  };

  const categoryOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
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

  const interactiveSelector = [
    // Plain 'a' (not just 'a[href]') — many real apps (SauceDemo's cart icon
    // included) implement a clickable anchor purely via a JS onClick handler with
    // no href/role attribute at all, which 'a[href]' alone would never see.
    'a', 'button', 'input', 'select', 'textarea', 'summary',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[role="checkbox"]', '[role="radio"]', '[role="switch"]', '[contenteditable="true"]',
    '[onclick]'
  ].join(',');

  const elements = [];
  const seen = new Set();
  document.querySelectorAll(interactiveSelector).forEach((el, idx) => {
    if (seen.has(el)) return;
    seen.add(el);
    if (elements.length >= 120) return;

    const id = nextId('el');
    el.setAttribute('data-gemmaqa-id', id);
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type');
    const rect = el.getBoundingClientRect();
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

    elements.push({
      element_id: id,
      tag,
      role: el.getAttribute('role'),
      type,
      input_type: type,
      name: el.getAttribute('name'),
      id_attr: el.id || null,
      test_id: el.getAttribute('data-testid'),
      text: textOf(el).slice(0, 120),
      visible_text: textOf(el).slice(0, 120),
      aria_label: el.getAttribute('aria-label'),
      accessible_name: accessibleName(el),
      label: labelFor(el),
      href: el.getAttribute('href'),
      placeholder: el.getAttribute('placeholder'),
      is_visible: isVisible(el),
      is_enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      checked: (type === 'checkbox' || type === 'radio') ? !!el.checked : null,
      current_value: currentValue,
      available_options: options,
      category: categoryOf(el),
      nth: idx,
      bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      selector_hint: `[data-gemmaqa-id="${id}"]`,
    });
  });

  const forms = [];
  document.querySelectorAll('form').forEach((form) => {
    if (forms.length >= 10) return;
    const formId = nextId('form');
    form.setAttribute('data-gemmaqa-id', formId);
    const fields = [];
    let submitId = null;
    // Controls a framework builds out of divs count too. `page_state.forms` is
    // what the form workflow actually fills, so a field missing HERE is a field
    // GemmaQA cannot complete however well the Perception Engine sees it —
    // app/perception/dom_extractor.py has the same selector for the same reason,
    // and fixing only that one left this path still blind.
    //
    // Confirmed against OrangeHRM's Admin -> Add User form: its User Role and
    // Status pickers are <div tabindex="0"> with no ARIA at all, so a required
    // field the agent could never fill was simply absent from the model. It
    // submitted an incomplete form eight times over.
    const CUSTOM_CONTROL_SELECTOR = [
      '[role="combobox"]', '[role="listbox"]', '[role="radiogroup"]',
      '[role="switch"]', '[role="spinbutton"]', '[role="slider"]',
      '[role="searchbox"]', '[role="textbox"]',
      '[aria-haspopup="listbox"]', '[contenteditable="true"]',
      '[tabindex]:not(input):not(select):not(textarea):not(button):not(a)',
    ].join(', ');
    form.querySelectorAll('input, select, textarea, button, ' + CUSTOM_CONTROL_SELECTOR).forEach((field) => {
      let fieldId = field.getAttribute('data-gemmaqa-id');
      if (!fieldId) {
        fieldId = nextId('el');
        field.setAttribute('data-gemmaqa-id', fieldId);
      }
      const tag = field.tagName.toLowerCase();
      const type = (field.getAttribute('type') || tag).toLowerCase();
      const isSubmitLike = type === 'submit' || type === 'button' || type === 'reset' || tag === 'button';
      if (isSubmitLike && !submitId) submitId = fieldId;
      if (!['input', 'select', 'textarea', 'button'].includes(tag)) {
        // A widget wrapping a real input is driven through that input.
        if (field.querySelector('input, select, textarea')) return;
        const role = (field.getAttribute('role') || '').toLowerCase();
        const roleTypes = {
          combobox: 'select', listbox: 'select', radiogroup: 'radio',
          switch: 'checkbox', spinbutton: 'number', slider: 'range',
          searchbox: 'text', textbox: 'text',
        };
        fields.push({
          name: field.getAttribute('name'),
          field_type: roleTypes[role]
            || (field.getAttribute('aria-haspopup') === 'listbox' ? 'select' : 'text'),
          label: labelFor(field),
          required: field.getAttribute('aria-required') === 'true',
          placeholder: field.getAttribute('placeholder'),
          options: [],
          element_id: fieldId,
          disabled: field.getAttribute('aria-disabled') === 'true',
          role: role || null,
          multiple: field.getAttribute('aria-multiselectable') === 'true',
          list_attr: null,
          aria_autocomplete: field.getAttribute('aria-autocomplete'),
          // Whatever the widget displays IS its value to a tester; a div has no
          // `.value` to read.
          current_value: (field.textContent || '').trim().slice(0, 80),
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
          name: field.getAttribute('name'),
          field_type: type,
          label: labelFor(field),
          required: !!field.required,
          placeholder: field.getAttribute('placeholder'),
          options,
          element_id: fieldId,
          disabled: !!field.disabled,
          current_value: value,
          // Signals `_infer_field_kind` needs to tell a lookup from a plain text
          // box. Dropped here until now, so `field_kind` was always None on the
          // path the form workflow uses and a lookup was unrecognisable — see
          // app/perception/form_extractor.py, which collects the same signals.
          role: field.getAttribute('role'),
          multiple: !!field.multiple,
          list_attr: field.getAttribute('list'),
          aria_autocomplete: field.getAttribute('aria-autocomplete'),
        });
      }
    });
    forms.push({
      form_id: formId,
      action: form.getAttribute('action'),
      method: form.getAttribute('method'),
      fields,
      submit_element_id: submitId,
    });
  });

  const tables = [];
  document.querySelectorAll('table').forEach((table) => {
    if (tables.length >= 8) return;
    const tableId = nextId('table');
    table.setAttribute('data-gemmaqa-id', tableId);
    const headers = Array.from(table.querySelectorAll('th')).map(th => textOf(th)).filter(Boolean).slice(0, 20);
    const rows = Array.from(table.querySelectorAll('tr'));
    const sample = rows.slice(headers.length ? 1 : 0, (headers.length ? 1 : 0) + 3).map(row =>
      Array.from(row.querySelectorAll('td')).map(td => textOf(td).slice(0, 80))
    );
    tables.push({
      table_id: tableId,
      headers,
      row_count: Math.max(0, rows.length - (headers.length ? 1 : 0)),
      sample_rows: sample,
    });
  });

  const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
    .map(h => textOf(h)).filter(Boolean).slice(0, 20);

  const breadcrumbs = Array.from(
    document.querySelectorAll('nav[aria-label*="breadcrumb" i] li, .breadcrumb li, [class*="breadcrumb"] li')
  ).map(li => textOf(li)).filter(Boolean).slice(0, 12);

  const navigation_items = Array.from(
    document.querySelectorAll('nav a, [role="navigation"] a, header a')
  ).map(a => textOf(a) || a.getAttribute('aria-label') || '').filter(Boolean).slice(0, 30);

  const tabs = Array.from(document.querySelectorAll('[role="tab"]'))
    .map(t => textOf(t)).filter(Boolean).slice(0, 20);

  const dialogs = Array.from(document.querySelectorAll('dialog[open], [role="dialog"], [aria-modal="true"]'))
    .filter(isVisible)
    .map(d => textOf(d).slice(0, 160))
    .filter(Boolean)
    .slice(0, 10);

  const modals = Array.from(document.querySelectorAll('.modal.show, .modal[open], [class*="Modal"][aria-modal="true"]'))
    .filter(isVisible)
    .map(d => textOf(d).slice(0, 160))
    .filter(Boolean)
    .slice(0, 8);

  const toasts = Array.from(document.querySelectorAll('[role="status"], .toast, .Toast, [class*="toast"], [class*="snackbar"]'))
    .filter(isVisible)
    .map(t => textOf(t).slice(0, 120))
    .filter(Boolean)
    .slice(0, 10);

  // Error and validation messages are the application TELLING us what it
  // thinks went wrong, so they are the highest-value text on the page for a QA
  // agent. Matching only role="alert"/.alert missed the extremely common
  // `<span id="error">` and `.error-message` shapes — observed live, where an
  // application spelled out "phone: Phone number is invalid, street1: ...
  // longer than the maximum allowed length (40)" and GemmaQA saw nothing.
  // ARIA live regions are included because that is where accessible apps put
  // exactly this text.
  const alertNodes = Array.from(document.querySelectorAll(
    '[role="alert"], [role="status"], [aria-live="assertive"], [aria-live="polite"],' +
    '.alert, .Alert, [class*="error" i], [id*="error" i], [class*="invalid" i],' +
    '[class*="validation" i], [class*="danger" i], [class*="warning" i]'
  )).filter(isVisible);

  // Prefer the innermost match: a wrapper div and the span inside it would
  // otherwise report the same message twice.
  const alerts = alertNodes
    .filter(node => !alertNodes.some(other => other !== node && node.contains(other)))
    // 300 rather than 160: a multi-field validation summary names several
    // fields and is useless truncated to the first one.
    .map(a => textOf(a).slice(0, 300))
    .filter(Boolean)
    .filter((text, i, all) => all.indexOf(text) === i)
    .slice(0, 10);

  const pagination_controls = Array.from(
    document.querySelectorAll('[aria-label*="pagination" i] a, [aria-label*="pagination" i] button, .pagination a, .pagination button, nav[aria-label*="Page" i] a')
  ).map(el => textOf(el) || el.getAttribute('aria-label') || '').filter(Boolean).slice(0, 20);

  const search_fields = elements
    .filter(e => {
      const blob = `${e.name || ''} ${e.placeholder || ''} ${e.accessible_name || ''} ${e.input_type || ''}`.toLowerCase();
      return blob.includes('search') || e.input_type === 'search';
    })
    .map(e => e.accessible_name || e.placeholder || e.element_id)
    .slice(0, 10);

  const filter_controls = elements
    .filter(e => {
      const blob = `${e.name || ''} ${e.accessible_name || ''} ${e.placeholder || ''}`.toLowerCase();
      return /filter|facet|refine/.test(blob);
    })
    .map(e => e.accessible_name || e.name || e.element_id)
    .slice(0, 10);

  const disabled_controls = elements.filter(e => e.disabled).map(e => e.element_id).slice(0, 40);
  const required_fields = elements.filter(e => e.required).map(e => e.element_id).slice(0, 40);

  const bodyText = (document.body && document.body.innerText) ? document.body.innerText : '';

  return {
    title: document.title || '',
    headings,
    breadcrumbs,
    navigation_items,
    visible_text: bodyText.slice(0, 2500),
    interactive_elements: elements,
    forms,
    tables,
    tabs,
    dialogs,
    modals,
    toasts,
    alerts,
    pagination_controls,
    search_fields,
    filter_controls,
    disabled_controls,
    required_fields,
  };
}
""".replace("/*__LABEL_FOR__*/", label_resolution_js(max_chars=120))


def normalize_element(raw: dict[str, Any], max_options: int = MAX_OPTIONS) -> InteractiveElement:
    """Normalize a raw extracted element dict into InteractiveElement + locator strategy."""
    input_type = raw.get("input_type") or raw.get("type")
    visible = raw.get("visible_text") or raw.get("text")
    accessible = raw.get("accessible_name") or raw.get("aria_label") or raw.get("label")
    strategy = choose_locator_strategy(raw)
    options = list(raw.get("available_options") or raw.get("options") or [])[:max_options]

    # Never leak password values
    current_value = raw.get("current_value")
    if (input_type or "").lower() == "password" and current_value not in (None, "", "***"):
        current_value = "***"

    el = InteractiveElement(
        element_id=str(raw.get("element_id") or new_id()),
        tag=str(raw.get("tag") or "div").lower(),
        role=raw.get("role"),
        type=input_type,
        input_type=input_type,
        name=raw.get("name"),
        id_attr=raw.get("id_attr"),
        text=visible,
        visible_text=visible,
        aria_label=raw.get("aria_label"),
        accessible_name=accessible,
        label=raw.get("label"),
        href=raw.get("href"),
        placeholder=raw.get("placeholder"),
        is_visible=bool(raw.get("is_visible", True)),
        is_enabled=bool(raw.get("is_enabled", True)),
        required=bool(raw.get("required", False)),
        disabled=bool(raw.get("disabled", False)),
        checked=raw.get("checked"),
        current_value=current_value,
        available_options=options,
        bounding_box=raw.get("bounding_box"),
        selector_hint=raw.get("selector_hint") or strategy.selector,
        locator_strategy=strategy,
        category=raw.get("category"),
    )
    return el



def _form_field(raw: dict[str, Any]) -> FormField:
    """Build a FormField, deriving `field_kind` the SAME way the Perception
    Engine does.

    `field_kind` was set only in `app.perception.form_extractor`, which feeds the
    Canonical Page Model — while the form workflow fills `PageState.forms`, built
    here. So on the path that actually completes forms it was always None, and
    `field_constraint_inference`'s lookup branches (autocomplete / combobox ->
    autocomplete_selection / dropdown_option) were unreachable. A lookup was
    unrecognisable by construction, and the value generator invented a value for
    it.

    `_infer_field_kind` is imported rather than reimplemented: one rule, one
    implementation. Two copies of a rule caused this bug and two others like it.
    """
    from app.perception.form_extractor import _infer_field_kind

    data = dict(raw)
    signals = {
        "field_type": data.get("field_type"),
        "role": data.get("role"),
        "name": data.get("name"),
        "multiple": data.get("multiple"),
        "list_attr": data.get("list_attr"),
        "aria_autocomplete": data.get("aria_autocomplete"),
    }
    for key in ("role", "multiple", "list_attr", "aria_autocomplete"):
        data.pop(key, None)
    try:
        data["field_kind"] = _infer_field_kind(signals, checkbox_group_names=set())
    except Exception:  # pragma: no cover - classification must never break observation
        data["field_kind"] = None
    return FormField(**data)


class PageObserver:
    """Observe the current page and build a capped PageState + locator registry."""

    def __init__(
        self,
        console_monitor: ConsoleMonitor | None = None,
        network_monitor: NetworkMonitor | None = None,
        registry: LocatorRegistry | None = None,
        max_elements: int | None = None,
        text_max_chars: int | None = None,
    ) -> None:
        settings = get_settings()
        self.console_monitor = console_monitor or ConsoleMonitor()
        self.network_monitor = network_monitor or NetworkMonitor()
        self.registry = registry or LocatorRegistry()
        self.max_elements = max_elements or settings.observe_max_elements
        self.text_max_chars = text_max_chars or settings.observe_text_max_chars

    async def observe(self, page: Page, screenshot_path: str | None = None) -> PageState:
        # Pages that auto-redirect shortly after load (e.g. a logout confirmation
        # bouncing back to /login) can destroy the JS context mid-evaluate; settle
        # once and retry rather than failing the whole run on that race.
        try:
            raw = await page.evaluate(OBSERVE_SCRIPT)
        except Exception as exc:
            if "Execution context was destroyed" not in str(exc):
                raise
            logger.info("Observe raced a navigation; retrying after settle")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            raw = await page.evaluate(OBSERVE_SCRIPT)
        return self.build_page_state(
            raw=raw,
            url=page.url,
            screenshot_path=screenshot_path,
        )

    async def observe_adapter(
        self,
        adapter: Any,
        screenshot_path: str | None = None,
    ) -> PageState:
        """Build PageState from BrowserAdapter observation payload (MCP-safe)."""
        raw = await adapter.get_observation_payload()
        if not isinstance(raw, dict):
            raw = {}
        url = raw.get("url") or ""
        try:
            url = url or await adapter.get_current_url()
        except Exception:
            pass
        title = raw.get("title") or ""
        try:
            title = title or await adapter.get_page_title()
        except Exception:
            pass
        raw.setdefault("title", title)
        # Merge adapter console/network when monitors are empty
        state = self.build_page_state(
            raw=raw,
            url=str(url),
            screenshot_path=screenshot_path,
        )
        try:
            console_events = await adapter.get_console_events()
            network_events = await adapter.get_network_events()
            if console_events and not state.console_errors:
                state.console_errors = list(console_events)[:40]
            if network_events and not state.network_failures:
                state.network_failures = list(network_events)[:40]
        except Exception:
            pass
        state.state_fingerprint = fingerprint_page_state(state)
        return state

    def build_page_state(
        self,
        *,
        raw: dict[str, Any],
        url: str,
        screenshot_path: str | None = None,
    ) -> PageState:
        """Pure normalization path (also used by unit tests)."""
        self.registry.clear()
        elements: list[InteractiveElement] = []
        for item in (raw.get("interactive_elements") or [])[: self.max_elements]:
            el = normalize_element(item)
            self.registry.register(el)
            elements.append(el)

        forms = [
            FormDescriptor(
                form_id=f["form_id"],
                action=f.get("action"),
                method=f.get("method"),
                fields=[_form_field(field) for field in f.get("fields", [])],
                submit_element_id=f.get("submit_element_id"),
            )
            for f in (raw.get("forms") or [])[:MAX_FORMS]
        ]
        tables = [TableDescriptor(**t) for t in (raw.get("tables") or [])[:MAX_TABLES]]

        state = PageState(
            page_id=new_id(),
            url=url,
            title=raw.get("title") or "",
            headings=list(raw.get("headings") or [])[:MAX_HEADINGS],
            visible_text_summary=truncate_text(
                raw.get("visible_text") or "", self.text_max_chars
            ),
            breadcrumbs=list(raw.get("breadcrumbs") or [])[:12],
            navigation_items=list(raw.get("navigation_items") or [])[:30],
            interactive_elements=elements,
            forms=forms,
            tables=tables,
            tabs=list(raw.get("tabs") or [])[:20],
            dialogs=list(raw.get("dialogs") or [])[:10],
            modals=list(raw.get("modals") or [])[:8],
            toasts=list(raw.get("toasts") or [])[:10],
            alerts=list(raw.get("alerts") or [])[:10],
            pagination_controls=list(raw.get("pagination_controls") or [])[:20],
            search_fields=list(raw.get("search_fields") or [])[:10],
            filter_controls=list(raw.get("filter_controls") or [])[:10],
            disabled_controls=list(raw.get("disabled_controls") or [])[:40],
            required_fields=list(raw.get("required_fields") or [])[:40],
            console_errors=self.console_monitor.snapshot(),
            network_failures=self.network_monitor.snapshot(),
            console_entries=self.console_monitor.snapshot_entries(),
            network_entries=self.network_monitor.snapshot_entries(),
            screenshot_path=screenshot_path,
        )
        state.state_fingerprint = fingerprint_page_state(state)
        logger.info(
            "Observed page %s (%s elements, fp=%s)",
            state.url,
            len(elements),
            (state.state_fingerprint or "")[:10],
        )
        return state

"""Live verification harness for CRUD Surface Discovery — perception-only,
read-only.

Deliberately does NOT drive the full AgentController (no autonomous
clicking): it launches Chromium directly, optionally logs in with provided
credentials, observes ONE page with `PerceptionEngine`, and reports what the
new collection/form-intent/action-semantics/CRUD-hypothesis layers found.
No form is ever submitted beyond the login form itself, and no Add/Edit/
Delete control is ever clicked — this script only PERCEIVES, matching the
task's "do not perform destructive operations" instruction unconditionally
rather than relying on flags to prevent it.

Usage (from gemmaqa/backend, venv active):

    python scripts/crud_discovery_live_capture.py <url> \
        [--username U] [--password P] [--output out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.intelligence.crud_discovery.crud_candidate_builder import CRUDCandidateBuilder  # noqa: E402
from app.perception.engine import PerceptionEngine  # noqa: E402


def _collection_summary(model) -> list[dict[str, Any]]:
    return [
        {
            "collection_type": c.collection_type,
            "row_count": c.row_count,
            "columns": [col.header_text for col in c.columns],
            "structural_signals": c.structural_signals,
            "row_actions": [{"element_id": a.element_id, "semantic_action": a.semantic_action} for a in c.row_actions],
            "global_actions": [{"element_id": a.element_id, "semantic_action": a.semantic_action} for a in c.global_actions],
            "has_search_control": c.has_search_control,
            "has_filter_controls": c.has_filter_controls,
        }
        for c in model.collections
    ]


def _form_intent_summary(model) -> list[dict[str, Any]]:
    return [
        {
            "form_id": fi.form_id,
            "intent": fi.intent,
            "confidence": round(fi.confidence.value, 3),
            "evidence": fi.evidence,
            "alternatives": [{"intent": a.intent, "confidence": a.confidence} for a in fi.alternatives],
        }
        for fi in model.form_intents
    ]


async def _try_login(page, username: str | None, password: str | None) -> str:
    if not username or not password:
        return "skipped (no credentials provided)"
    try:
        pass_field = page.locator('input[type="password"]').first
        # Wait for the client-rendered login form to actually mount (SPA
        # targets take a moment after domcontentloaded) before any count()
        # check, which does not itself auto-retry.
        await pass_field.wait_for(state="visible", timeout=15000)
        user_field = page.locator(
            'input[name="username"], input[name="email"], input[type="email"], #username, #email, '
            'input[id*="user" i], input[name*="user" i]'
        ).first
        if await user_field.count() == 0:
            # Generic fallback: the first visible text-ish input on the page
            # (login forms overwhelmingly have exactly one before the
            # password field) — never a target-application-specific selector.
            user_field = page.locator('input:not([type="password"]):not([type="hidden"]):not([type="checkbox"])').first
        await user_field.fill(username, timeout=10000)
        await pass_field.fill(password, timeout=10000)
        submit = page.locator('button[type="submit"], input[type="submit"], button:has-text("Login"), button:has-text("Log In"), button:has-text("Sign In")').first
        await submit.click(timeout=10000)
        await page.wait_for_load_state("networkidle", timeout=15000)
        return "attempted"
    except Exception as exc:
        return f"failed: {type(exc).__name__}: {exc}"


async def run(url: str, *, username: str | None, password: str | None, after_login_path: str | None = None) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            login_result = await _try_login(page, username, password)
            if after_login_path:
                base = f"{page.url.split('/', 3)[0]}//{page.url.split('/', 3)[2]}"
                await page.goto(base + after_login_path, wait_until="networkidle", timeout=20000)

            engine = PerceptionEngine()
            model = await engine.observe(page)

            candidate_builder = CRUDCandidateBuilder()
            hypotheses = candidate_builder.build_entry_points(model)

            return {
                "url": url,
                "final_url": page.url,
                "title": model.title,
                "login_result": login_result,
                "tables_native": len(model.tables),
                "collections": _collection_summary(model),
                "form_intents": _form_intent_summary(model),
                "action_semantics_count": len(model.action_semantics),
                "crud_entry_point_hypotheses": [h.to_summary_dict() for h in hypotheses],
            }
        finally:
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--after-login-path", default=None)
    args = parser.parse_args()

    result = asyncio.run(run(args.url, username=args.username, password=args.password, after_login_path=args.after_login_path))
    text = json.dumps(result, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

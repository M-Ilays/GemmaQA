#!/usr/bin/env python3
"""
Real Gemma action test (does not use mock unless configured).

Sends a fixture page state, requests one action, validates it, prints a safe summary.

Usage (from gemmaqa/):
  backend\\.venv\\Scripts\\python.exe scripts\\test_gemma_action.py

Requires a configured provider, for example:
  GEMMA_PROVIDER=openai_compatible
  GEMMA_API_BASE_URL=http://127.0.0.1:11434/v1
  GEMMA_MODEL_ID=<your-model-id>
  GEMMA_TEMPERATURE=0
  GEMMA_MAX_OUTPUT_TOKENS=512
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# Ensure backend cwd for .env loading
os.chdir(BACKEND)

from app.config import get_settings  # noqa: E402
from app.gemma import get_gemma_provider, reset_gemma_provider  # noqa: E402
from app.gemma.base import ActionGenerationRequest  # noqa: E402
from app.gemma.parser import parse_and_validate_action  # noqa: E402
from app.schemas import InteractiveElement, PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402
from app.utils.sanitization import MASK  # noqa: E402


def fixture_page() -> PageState:
    return PageState(
        page_id=new_id(),
        url="https://app.example.com/customers",
        title="Customers",
        headings=["Customers", "Filters"],
        visible_text_summary="Customer list with search and create button.",
        interactive_elements=[
            InteractiveElement(
                element_id="el_nav_dashboard",
                tag="a",
                category="link",
                accessible_name="Dashboard",
                href="/dashboard",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_btn_create",
                tag="button",
                category="button",
                accessible_name="Add customer",
                text="Add customer",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_search",
                tag="input",
                category="input",
                input_type="search",
                accessible_name="Search customers",
                placeholder="Search",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_password",
                tag="input",
                category="input",
                input_type="password",
                accessible_name="Password",
                current_value="SHOULD_NEVER_REACH_MODEL",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )


async def main() -> int:
    settings = get_settings()
    reset_gemma_provider()
    provider = get_gemma_provider(force_new=True)

    print("=== GemmaQA action probe ===")
    print(f"provider_env={settings.gemma_provider}")
    print(f"provider_normalized={settings.normalized_gemma_provider}")
    print(f"provider_instance={provider.name}")
    print(f"model_id={settings.effective_gemma_model_id or '(unset)'}")
    print(f"api_base={settings.effective_gemma_api_base or '(unset)'}")
    print(f"supports_images={settings.effective_gemma_supports_images}")
    print(f"temperature={settings.gemma_temperature}")
    print(f"max_output_tokens={settings.effective_gemma_max_tokens}")
    # Never print API key
    print(f"api_key_set={bool(settings.gemma_api_key)}")

    if settings.normalized_gemma_provider == "mock":
        print(
            "\nNOTE: GEMMA_PROVIDER=mock — this script will exercise the mock path.\n"
            "Set GEMMA_PROVIDER=openai_compatible|transformers for a real model call."
        )

    ok = await provider.health_check()
    health = provider.public_health()
    print("health:", json.dumps(health, indent=2))
    if not ok and settings.normalized_gemma_provider != "mock":
        print("WARNING: health_check returned false — generation may still be attempted")

    page = fixture_page()
    request = ActionGenerationRequest(
        page_state=page,
        testing_objective="Explore the customers module safely",
        remaining_action_budget=10,
        remaining_page_budget=5,
        safe_mode=True,
        authorized_domain="app.example.com",
    )

    try:
        action = await provider.generate_action(request)
    except Exception as exc:
        print(f"FAIL: generate_action raised {type(exc).__name__}: {exc}")
        return 2

    # Validate again against known ids
    known = {el.element_id for el in page.interactive_elements}
    try:
        parse_and_validate_action(
            {
                "action": action.action.value,
                "element_id": action.element_id,
                "value": action.value,
                "reason": action.reason,
                "expected_result": action.expected_result,
                "risk": action.risk.value if action.risk else "low",
                "category": action.category.value if action.category else "exploration",
            },
            known_element_ids=known,
            reject_high_risk=True,
        )
    except Exception as exc:
        # fallback finish actions may omit element ids — allow finish
        if action.action.value != "finish":
            print(f"FAIL: action validation: {exc}")
            return 1

    summary = {
        "action": action.action.value,
        "element_id": action.element_id,
        "risk": action.risk.value if action.risk else None,
        "category": action.category.value if action.category else None,
        "reason": (action.reason or "")[:160],
        "expected_result": (action.expected_result or "")[:160],
        "metadata_keys": sorted((action.metadata or {}).keys()),
        "password_leaked": "SHOULD_NEVER_REACH_MODEL" in json.dumps(action.model_dump(mode="json")),
        "mask_token": MASK,
    }
    print("\nSAFE SUMMARY:")
    print(json.dumps(summary, indent=2))
    if summary["password_leaked"]:
        print("FAIL: password value leaked into action payload")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

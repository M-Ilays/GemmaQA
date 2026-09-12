"""Playwright MCP adapter — tool gateway; never exposed directly to Gemma."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from app.browser.adapters.base import BrowserAdapter
from app.browser.adapters.types import AdapterCapabilities, TargetRef
from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger("browser.adapter.mcp")


class PlaywrightMCPAdapter(BrowserAdapter):
    """
    Browser execution via official Playwright MCP (stdio or HTTP URL).

    Gemma cannot call MCP tools directly. Only the custom QA executor
    invokes this adapter after safety validation.
    """

    name = "playwright_mcp"

    def __init__(
        self,
        run_id: str,
        *,
        transport: "McpTransport | None" = None,
        capabilities_override: AdapterCapabilities | None = None,
    ) -> None:
        self.run_id = run_id
        settings = get_settings()
        self.timeout = float(settings.playwright_mcp_timeout_seconds)
        self._transport = transport
        self._owns_transport = transport is None
        self._console: list[str] = []
        self._network: list[str] = []
        self._current_url = ""
        self._title = ""
        self._started = False
        self._element_refs: dict[str, str] = {}  # target_id → MCP ref
        self._last_error: str | None = None
        self._caps = capabilities_override or AdapterCapabilities(
            # Official MCP supports core actions; console/network vary by version
            console_events=False,
            network_events=False,
            tabs=True,
            hover=True,
            press=True,
            check=True,
            reload=True,
        )
        self._known_tools: set[str] = set()

    def capabilities(self) -> AdapterCapabilities:
        return self._caps

    async def start_session(self) -> None:
        try:
            if self._transport is None:
                self._transport = await create_mcp_transport()
            await self._transport.initialize()
            tools = await self._transport.list_tools()
            self._known_tools = {
                str(t.get("name") or "") for t in tools if isinstance(t, dict)
            }
            self._started = True
            self._last_error = None
            logger.info("Browser adapter active: Playwright MCP (run=%s)", self.run_id)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(
                f"Playwright MCP unavailable: {self._last_error}. "
                "Refusing silent fallback to Direct Playwright."
            ) from exc

    async def close_session(self) -> None:
        if self._transport and self._owns_transport:
            await self._transport.close()
        self._transport = None
        self._started = False

    def _require(self) -> "McpTransport":
        if not self._transport or not self._started:
            raise RuntimeError("Playwright MCP session is not started or was lost")
        return self._transport

    async def _call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        if self._known_tools and tool not in self._known_tools:
            raise RuntimeError(f"Unsupported MCP tool: {tool}")
        try:
            return await self._require().call_tool(tool, arguments or {})
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            msg = str(exc).lower()
            if "expired" in msg or "stale" in msg:
                raise RuntimeError(f"Element reference expired: {exc}") from exc
            if "closed" in msg or "disconnect" in msg or "not running" in msg:
                self._started = False
                raise RuntimeError(f"MCP session lost: {exc}") from exc
            raise

    async def navigate(self, url: str) -> str:
        await self._call("browser_navigate", {"url": url})
        self._current_url = url
        await self.wait_for_page_stable()
        return await self.get_current_url()

    async def go_back(self) -> str:
        await self._call("browser_navigate_back", {})
        await self.wait_for_page_stable()
        return await self.get_current_url()

    async def reload(self) -> str:
        # Prefer evaluate reload when dedicated tool missing
        try:
            await self._call("browser_navigate", {"url": self._current_url or "about:blank"})
        except Exception:
            await self._call("browser_evaluate", {"function": "() => location.reload()"})
        await self.wait_for_page_stable()
        return await self.get_current_url()

    async def resolve_target(self, target: TargetRef) -> TargetRef:
        # Refresh snapshot mapping when needed
        if target.target_id and target.target_id not in self._element_refs:
            await self._refresh_refs()
        if target.target_id and target.target_id in self._element_refs:
            return target
        if target.name or target.selector_hint:
            # Allow name-based actions without prior ref
            return target
        raise ValueError(f"Target not found: {target.display()}")

    async def element_exists(self, target: TargetRef) -> bool:
        try:
            await self.resolve_target(target)
            return True
        except Exception:
            return False

    def _ref_args(self, target: TargetRef) -> dict[str, Any]:
        ref = ""
        if target.target_id:
            ref = self._element_refs.get(target.target_id, "")
        if not ref:
            ref = target.selector_hint or target.name or target.target_id or ""
        return {
            "element": target.name or target.target_id or ref,
            "ref": ref,
        }

    async def click_target(self, target: TargetRef) -> None:
        await self.resolve_target(target)
        await self._call("browser_click", self._ref_args(target))

    async def fill_target(self, target: TargetRef, value: str) -> None:
        await self.resolve_target(target)
        args = {**self._ref_args(target), "text": value}
        await self._call("browser_type", args)

    async def select_target(self, target: TargetRef, value: str) -> None:
        await self.resolve_target(target)
        args = {**self._ref_args(target), "values": [value]}
        await self._call("browser_select_option", args)

    async def check_target(self, target: TargetRef, checked: bool = True) -> None:
        # MCP often uses click for checkboxes
        await self.click_target(target)

    async def hover_target(self, target: TargetRef) -> None:
        await self.resolve_target(target)
        await self._call("browser_hover", self._ref_args(target))

    async def press_target(self, target: TargetRef, key: str) -> None:
        await self.resolve_target(target)
        args = {**self._ref_args(target), "key": key}
        try:
            await self._call("browser_press_key", args)
        except Exception:
            await self._call("browser_type", {**self._ref_args(target), "text": "", "submit": True})

    async def take_screenshot(self, path: str, *, full_page: bool = False) -> str:
        result = await self._call(
            "browser_take_screenshot",
            {"filename": path, "fullPage": full_page},
        )
        if isinstance(result, dict) and result.get("path"):
            return str(result["path"])
        # Ensure file exists for evidence pipeline
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"")  # placeholder when MCP returns inline only
        return path

    async def get_current_url(self) -> str:
        try:
            result = await self._call("browser_evaluate", {"function": "() => location.href"})
            if isinstance(result, str) and result.startswith(("http", "about:")):
                self._current_url = result
            elif isinstance(result, dict) and result.get("result"):
                self._current_url = str(result["result"])
        except Exception:
            pass
        return self._current_url

    async def get_page_title(self) -> str:
        try:
            result = await self._call("browser_evaluate", {"function": "() => document.title"})
            if isinstance(result, str):
                self._title = result
            elif isinstance(result, dict) and result.get("result") is not None:
                self._title = str(result["result"])
        except Exception:
            pass
        return self._title

    async def get_accessibility_snapshot(self) -> dict[str, Any] | str:
        result = await self._call("browser_snapshot", {})
        if isinstance(result, (dict, str)):
            return result
        return {"raw": str(result)}

    async def get_observation_payload(self) -> dict[str, Any]:
        snap = await self.get_accessibility_snapshot()
        payload = snapshot_to_observation(snap, url=self._current_url, title=self._title)
        # Update refs from observation
        for el in payload.get("interactive_elements") or []:
            eid = el.get("element_id")
            ref = el.get("mcp_ref") or el.get("selector_hint") or eid
            if eid and ref:
                self._element_refs[str(eid)] = str(ref)
        if payload.get("title"):
            self._title = str(payload["title"])
        if payload.get("url"):
            self._current_url = str(payload["url"])
        return payload

    async def _refresh_refs(self) -> None:
        await self.get_observation_payload()

    async def get_console_events(self) -> list[str]:
        if not self._caps.console_events:
            return list(self._console)
        try:
            result = await self._call("browser_console_messages", {})
            if isinstance(result, list):
                self._console = [str(x) for x in result]
            elif isinstance(result, dict) and "messages" in result:
                self._console = [str(x) for x in result["messages"]]
        except Exception:
            pass
        return list(self._console)

    async def get_network_events(self) -> list[str]:
        if not self._caps.network_events:
            return list(self._network)
        try:
            result = await self._call("browser_network_requests", {})
            if isinstance(result, list):
                self._network = [str(x) for x in result]
        except Exception:
            pass
        return list(self._network)

    async def wait_for_page_stable(self, timeout_ms: int = 3000) -> None:
        await self.wait(min(400, timeout_ms))

    async def wait(self, ms: int) -> None:
        await asyncio.sleep(max(0, ms) / 1000.0)

    async def list_tabs(self) -> list[dict[str, Any]]:
        try:
            result = await self._call("browser_tabs", {"action": "list"})
            if isinstance(result, list):
                return [t if isinstance(t, dict) else {"tab_id": str(i)} for i, t in enumerate(result)]
            if isinstance(result, dict) and "tabs" in result:
                return list(result["tabs"])
        except Exception:
            pass
        return [{"tab_id": "0", "url": self._current_url, "title": self._title}]

    async def switch_tab(self, tab_id: str) -> None:
        await self._call("browser_tabs", {"action": "select", "index": int(tab_id)})

    async def health_check(self) -> dict[str, Any]:
        settings = get_settings()
        info: dict[str, Any] = {
            "adapter": self.name,
            "available": False,
            "session_active": self._started,
            "transport": "http" if settings.playwright_mcp_url else "stdio",
            "mcp_url": settings.playwright_mcp_url or None,
            "capabilities": self.capabilities().to_dict(),
            "last_error": self._last_error,
        }
        if not self._started:
            try:
                transport = await create_mcp_transport()
                await transport.initialize()
                tools = await transport.list_tools()
                await transport.close()
                info["available"] = True
                info["tools"] = len(tools)
            except Exception as exc:
                info["error"] = f"{type(exc).__name__}: MCP unavailable"
                self._last_error = info["error"]
            return info
        try:
            tools = await self._require().list_tools()
            info["available"] = True
            info["tools"] = len(tools)
        except Exception as exc:
            info["error"] = f"{type(exc).__name__}: health failed"
            self._last_error = info["error"]
        return info


def snapshot_to_observation(
    snap: dict[str, Any] | str,
    *,
    url: str = "",
    title: str = "",
) -> dict[str, Any]:
    """Convert MCP/a11y snapshot into PageObserver-compatible raw payload."""
    if isinstance(snap, dict) and snap.get("interactive_elements"):
        # Already structured (tests / enhanced MCP)
        out = dict(snap)
        out.setdefault("url", url or snap.get("url") or "")
        out.setdefault("title", title or snap.get("title") or "")
        return out

    elements: list[dict[str, Any]] = []
    counter = 0

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal counter
        if not isinstance(node, dict):
            return
        role = str(node.get("role") or node.get("type") or "")
        name = str(node.get("name") or node.get("text") or "")[:120]
        ref = str(node.get("ref") or node.get("nth") or "")
        interesting = role.lower() in {
            "button",
            "link",
            "textbox",
            "searchbox",
            "checkbox",
            "radio",
            "combobox",
            "listbox",
            "tab",
            "menuitem",
            "option",
        } or bool(node.get("href"))
        if interesting and (name or ref):
            counter += 1
            eid = f"el_{counter:03d}"
            tag = "a" if role == "link" else "button" if role == "button" else "input"
            category = (
                "link"
                if role == "link"
                else "button"
                if role == "button"
                else "input"
                if "text" in role or role in {"textbox", "searchbox"}
                else "other"
            )
            elements.append(
                {
                    "element_id": eid,
                    "tag": tag,
                    "role": role or None,
                    "accessible_name": name,
                    "visible_text": name,
                    "text": name,
                    "category": category,
                    "is_visible": True,
                    "is_enabled": True,
                    "selector_hint": ref or name,
                    "mcp_ref": ref or name,
                    "href": node.get("url") or node.get("href"),
                }
            )
        for child in node.get("children") or []:
            walk(child, depth + 1)

    if isinstance(snap, dict):
        title = title or str(snap.get("title") or "")
        url = url or str(snap.get("url") or "")
        if "root" in snap:
            walk(snap["root"])
        else:
            walk(snap)
        # Flat list of nodes
        for node in snap.get("nodes") or []:
            walk(node)
    elif isinstance(snap, str):
        # YAML-ish lines with role + name — extract simple patterns
        import re

        for m in re.finditer(
            r"-\s*(button|link|textbox|checkbox|tab)\s+[\"']?([^\"'\n]+)[\"']?",
            snap,
            re.I,
        ):
            counter += 1
            role, name = m.group(1).lower(), m.group(2).strip()
            elements.append(
                {
                    "element_id": f"el_{counter:03d}",
                    "tag": "button" if role == "button" else "a" if role == "link" else "input",
                    "role": role,
                    "accessible_name": name,
                    "visible_text": name,
                    "text": name,
                    "category": role if role in {"button", "link"} else "input",
                    "is_visible": True,
                    "is_enabled": True,
                    "selector_hint": name,
                    "mcp_ref": name,
                }
            )

    headings = []
    if isinstance(snap, dict):
        headings = list(snap.get("headings") or [])[:20]

    return {
        "title": title,
        "url": url,
        "headings": headings,
        "visible_text": title,
        "interactive_elements": elements[:100],
        "forms": list((snap.get("forms") if isinstance(snap, dict) else None) or []),
        "tables": [],
        "tabs": [],
        "dialogs": [],
        "modals": [],
        "toasts": [],
        "alerts": [],
        "navigation_items": [
            e.get("accessible_name")
            for e in elements
            if e.get("category") in {"link", "button"}
        ][:30],
        "breadcrumbs": [],
        "pagination_controls": [],
        "search_fields": [],
        "filter_controls": [],
        "disabled_controls": [],
        "required_fields": [],
    }


class McpTransport:
    """Minimal MCP tools/call client (stdio JSON-RPC or injectable fake)."""

    async def initialize(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def list_tools(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raise NotImplementedError


class FakeMcpTransport(McpTransport):
    """Deterministic transport for unit tests."""

    def __init__(self, *, disconnect_after: int | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.url = "about:blank"
        self.title = "Fake Page"
        self._disconnect_after = disconnect_after
        self._call_count = 0
        self._closed = False
        self._fields: dict[str, str] = {}
        self._tools = [
            {"name": "browser_navigate"},
            {"name": "browser_click"},
            {"name": "browser_type"},
            {"name": "browser_select_option"},
            {"name": "browser_snapshot"},
            {"name": "browser_take_screenshot"},
            {"name": "browser_navigate_back"},
            {"name": "browser_evaluate"},
            {"name": "browser_hover"},
            {"name": "browser_press_key"},
            {"name": "browser_tabs"},
        ]

    async def initialize(self) -> None:
        self._closed = False
        return None

    async def close(self) -> None:
        self._closed = True
        return None

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._closed:
            raise RuntimeError("MCP process not running")
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._closed:
            raise RuntimeError("MCP process not running")
        self._call_count += 1
        if self._disconnect_after is not None and self._call_count > self._disconnect_after:
            self._closed = True
            raise RuntimeError("MCP process not running")
        self.calls.append((name, dict(arguments)))
        if name == "browser_navigate":
            self.url = str(arguments.get("url") or self.url)
            if "/signup" in self.url or "addUser" in self.url:
                self.title = "Sign Up"
            elif "/login" in self.url:
                self.title = "Login"
            else:
                self.title = "Home"
            return {"ok": True}
        if name == "browser_navigate_back":
            self.url = "https://example.com/"
            self.title = "Home"
            return {"ok": True}
        if name == "browser_click":
            ref = str(arguments.get("ref") or arguments.get("element") or "")
            if "sign" in ref.lower() or "signup" in ref.lower():
                self.url = "https://example.com/signup"
                self.title = "Sign Up"
            return {"ok": True}
        if name == "browser_type":
            ref = str(arguments.get("ref") or "")
            self._fields[ref] = str(arguments.get("text") or "")
            return {"ok": True}
        if name == "browser_select_option":
            return {"ok": True}
        if name == "browser_evaluate":
            fn = str(arguments.get("function") or "")
            if "title" in fn:
                return self.title
            return self.url
        if name == "browser_snapshot":
            return {
                "url": self.url,
                "title": self.title,
                "headings": [self.title],
                "interactive_elements": [
                    {
                        "element_id": "el_001",
                        "tag": "a",
                        "role": "link",
                        "accessible_name": "Sign up",
                        "visible_text": "Sign up",
                        "text": "Sign up",
                        "category": "link",
                        "is_visible": True,
                        "is_enabled": True,
                        "href": "/signup",
                        "selector_hint": "Sign up",
                        "mcp_ref": "Sign up",
                    },
                    {
                        "element_id": "el_002",
                        "tag": "input",
                        "role": "textbox",
                        "accessible_name": "Email",
                        "visible_text": "Email",
                        "text": "Email",
                        "category": "input",
                        "input_type": "email",
                        "is_visible": True,
                        "is_enabled": True,
                        "selector_hint": "Email",
                        "mcp_ref": "Email",
                    },
                    {
                        "element_id": "el_003",
                        "tag": "button",
                        "role": "button",
                        "accessible_name": "Submit",
                        "visible_text": "Submit",
                        "text": "Submit",
                        "category": "button",
                        "is_visible": True,
                        "is_enabled": True,
                        "selector_hint": "Submit",
                        "mcp_ref": "Submit",
                    },
                ],
                "forms": [
                    {
                        "form_id": "form_001",
                        "fields": [
                            {
                                "element_id": "el_002",
                                "label": "Email",
                                "field_type": "email",
                                "required": True,
                            }
                        ],
                    }
                ],
            }
        if name == "browser_take_screenshot":
            from pathlib import Path

            path = arguments.get("filename")
            if path:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
            return {"path": path}
        if name == "browser_tabs":
            return [{"tab_id": "0", "url": self.url, "title": self.title}]
        return {"ok": True}


class StdioMcpTransport(McpTransport):
    """Spawn Playwright MCP via stdio and speak JSON-RPC 2.0."""

    def __init__(self, command: str, args: list[str], timeout: float = 60.0) -> None:
        self.command = command
        self.args = args
        self.timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        # MCP initialize handshake
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "gemmaqa", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized", {})

    async def close(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._rpc("tools/list", {})
        return list((result or {}).get("tools") or [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict) and "content" in result:
            # Unwrap text content blocks when present
            parts = result.get("content") or []
            texts = [
                p.get("text")
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
            ]
            if len(texts) == 1:
                try:
                    return json.loads(texts[0])
                except Exception:
                    return texts[0]
            if texts:
                return texts
        return result

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        data = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        if not self._proc or not self._proc.stdin or not self._proc.stdout:
            raise RuntimeError("MCP process not running")
        async with self._lock:
            self._id += 1
            req_id = self._id
            msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            payload = (json.dumps(msg) + "\n").encode("utf-8")
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()
            deadline = asyncio.get_event_loop().time() + self.timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"MCP RPC timeout for {method}")
                line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
                if not line:
                    raise RuntimeError("MCP process closed stdout")
                try:
                    data = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if data.get("id") != req_id:
                    continue
                if "error" in data:
                    err = data["error"]
                    raise RuntimeError(f"MCP error: {err}")
                return data.get("result")


class HttpMcpTransport(McpTransport):
    """HTTP MCP client for servers started with --port (streamable HTTP / JSON)."""

    def __init__(self, url: str, timeout: float = 60.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._initialized = False

    async def initialize(self) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # Best-effort initialize; some gateways accept tools/call without handshake
            try:
                await client.post(
                    self.url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "gemmaqa", "version": "0.1.0"},
                        },
                    },
                )
            except Exception as exc:
                logger.warning("MCP HTTP initialize soft-fail: %s", type(exc).__name__)
        self._initialized = True

    async def close(self) -> None:
        self._initialized = False

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._rpc("tools/list", {})
        return list((result or {}).get("tools") or [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._rpc("tools/call", {"name": name, "arguments": arguments})

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                self.url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"MCP HTTP {resp.status_code}")
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"MCP error: {data['error']}")
            return data.get("result")


async def create_mcp_transport() -> McpTransport:
    settings = get_settings()
    if settings.playwright_mcp_url:
        return HttpMcpTransport(
            settings.playwright_mcp_url,
            timeout=float(settings.playwright_mcp_timeout_seconds),
        )
    return StdioMcpTransport(
        settings.playwright_mcp_command,
        settings.playwright_mcp_arg_list,
        timeout=float(settings.playwright_mcp_timeout_seconds),
    )

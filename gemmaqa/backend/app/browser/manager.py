"""Playwright browser lifecycle manager with tracing and login helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.config import get_settings
from app.schemas import LoginResult
from app.utils.logging import get_logger


def _chromium_launch_args() -> list[str]:
    """Extra Chromium flags required inside Docker / Cloud Run.

    Local Windows/macOS `python run.py` is unchanged (no extra args). Cloud Run
    and the production image have no sandbox user namespace and a tiny /dev/shm.
    """
    if os.getenv("PLAYWRIGHT_DOCKER") == "1" or Path("/.dockerenv").exists():
        return ["--no-sandbox", "--disable-dev-shm-usage"]
    return []

if TYPE_CHECKING:
    from app.browser.evidence import EvidenceCollector

logger = get_logger("browser.manager")


class BrowserManager:
    """Owns Playwright, browser, context, and page instances for a QA run."""

    def __init__(
        self,
        run_id: str | None = None,
        *,
        headless: bool | None = None,
        viewport_width: int | None = None,
        viewport_height: int | None = None,
        action_timeout_ms: int | None = None,
        navigation_timeout_ms: int | None = None,
        enable_tracing: bool | None = None,
    ) -> None:
        settings = get_settings()
        self.run_id = run_id or "adhoc"
        self.headless = settings.playwright_headless if headless is None else headless
        self.viewport_width = viewport_width or settings.viewport_width
        self.viewport_height = viewport_height or settings.viewport_height
        self.navigation_timeout = navigation_timeout_ms or settings.navigation_timeout_ms
        self.action_timeout = action_timeout_ms or settings.action_timeout_ms
        self.enable_tracing = settings.enable_tracing if enable_tracing is None else enable_tracing

        self._playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.trace_path: Path | None = None
        self._tracing = False
        self._started = False
        self._crashed = False

    async def start(self) -> Page:
        """Start Playwright, launch Chromium, create isolated context + primary page."""
        if self._started and self.page and not self.page.is_closed():
            return self.page

        settings = get_settings()
        run_root = settings.run_evidence_dir(self.run_id)
        self.trace_path = run_root / "traces" / f"trace_{self.run_id[:8]}.zip"

        try:
            try:
                self._playwright = await async_playwright().start()
            except NotImplementedError as exc:
                raise RuntimeError(
                    "Playwright failed to start Chromium on this event loop. "
                    "On Windows, restart the API with `python run.py` "
                    "(do not use uvicorn --reload). "
                    "GemmaQA sets WindowsProactorEventLoopPolicy for subprocess support."
                ) from exc
            self.browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=_chromium_launch_args(),
            )
            self.context = await self.browser.new_context(
                viewport={"width": self.viewport_width, "height": self.viewport_height},
                ignore_https_errors=True,
            )
            self.context.set_default_navigation_timeout(self.navigation_timeout)
            self.context.set_default_timeout(self.action_timeout)

            if self.enable_tracing:
                # `screenshots=True` starts a CDP screencast that runs for the WHOLE
                # run, not once per action. Counted in the trace from run 5b4268e0:
                # 1987 JPEG frames over 521s — 3.8 captures per second, 62.6MB — and
                # in a headed browser that continuous surface capture is what the
                # operator sees as the page blinking non-stop. It was invisible to
                # every earlier measurement because the page itself feels nothing
                # (0 viewport/scroll/visibility changes) and discrete
                # `page.screenshot()` calls cost nothing (0 dropped frames).
                #
                # A screencast exists so the run can be watched back later. When the
                # browser is headed the operator is watching it live, so the frames
                # are redundant with what is already on screen — and they cost the
                # flicker plus most of a 120MB trace. Headless runs keep them,
                # because there the frames are the only visual record there is.
                trace_screenshots = self.headless
                await self.context.tracing.start(
                    screenshots=trace_screenshots, snapshots=True, sources=False
                )
                self._tracing = True
                logger.info(
                    "Tracing started (screencast=%s, dom_snapshots=True)%s",
                    trace_screenshots,
                    "" if trace_screenshots
                    else " — screencast off for a headed run so the live window "
                         "does not flicker; DOM snapshots still recorded",
                )

            self.page = await self.context.new_page()
            self.page.on("crash", self._on_page_crash)
            self._started = True
            self._crashed = False
            logger.info(
                "Chromium launched (headless=%s, viewport=%sx%s, run=%s)",
                self.headless,
                self.viewport_width,
                self.viewport_height,
                self.run_id,
            )
            return self.page
        except Exception:
            await self.stop()
            raise

    def _on_page_crash(self, _page: Page) -> None:
        self._crashed = True
        logger.error("Primary page crashed for run %s", self.run_id)

    @property
    def crashed(self) -> bool:
        return self._crashed

    async def login(
        self,
        url: str,
        username: str | None,
        password: str | None,
        username_selector: str | None = None,
        password_selector: str | None = None,
        submit_selector: str | None = None,
        wait_after_ms: int = 2000,
        evidence: EvidenceCollector | None = None,
    ) -> LoginResult:
        """
        Deterministic login using optional selectors, with safe auto-detection fallback.

        Does not invent credentials — username/password must be provided.
        """
        page = self.require_page()
        if not username or not password:
            return LoginResult(
                success=False,
                method="skipped",
                message="Credentials not provided",
                before_url=page.url,
                after_url=page.url,
            )

        before_url = page.url
        screenshot_path: str | None = None
        detected: dict[str, str] = {}

        try:
            await page.goto(url, wait_until="domcontentloaded")
            before_url = page.url

            method: str
            if username_selector and password_selector:
                user_sel = username_selector
                pass_sel = password_selector
                submit_sel = submit_selector or 'button[type="submit"], input[type="submit"]'
                method = "selectors"
            else:
                detected = await self._detect_login_selectors(page)
                if username_selector:
                    detected["username"] = username_selector
                if password_selector:
                    detected["password"] = password_selector
                if submit_selector:
                    detected["submit"] = submit_selector
                if "username" not in detected or "password" not in detected:
                    raise RuntimeError(
                        "Could not auto-detect login fields; provide username/password selectors"
                    )
                user_sel = detected["username"]
                pass_sel = detected["password"]
                submit_sel = detected.get("submit") or 'button[type="submit"], input[type="submit"]'
                method = "auto" if not (username_selector and password_selector) else "selectors"
                if username_selector and password_selector:
                    method = "selectors"

            detected = {
                "username": user_sel,
                "password": pass_sel,
                "submit": submit_sel,
            }

            await page.locator(user_sel).first.fill(username)
            await page.locator(pass_sel).first.fill(password)
            await page.locator(submit_sel).first.click()
            await page.wait_for_timeout(wait_after_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            if evidence:
                shot = await evidence.screenshot(page, "login_result")
                screenshot_path = shot.path

            logger.info("Login attempt completed (%s) for %s", method, url)
            return LoginResult(
                success=True,
                method=method,  # type: ignore[arg-type]
                message="Login submitted",
                before_url=before_url,
                after_url=page.url,
                screenshot_path=screenshot_path,
                detected_selectors=detected,
            )
        except Exception as exc:
            logger.warning("Login failed: %s", exc)
            if evidence:
                try:
                    shot = await evidence.screenshot(page, "login_failed")
                    screenshot_path = shot.path
                except Exception:
                    pass
            return LoginResult(
                success=False,
                method="selectors" if username_selector else "auto",
                message="Login failed",
                before_url=before_url,
                after_url=page.url if self.page and not self.page.is_closed() else before_url,
                screenshot_path=screenshot_path,
                detected_selectors=detected,
                error=str(exc),
            )

    async def _detect_login_selectors(self, page: Page) -> dict[str, str]:
        """Detect email/username, password, and submit controls without guessing credentials."""
        return await page.evaluate(
            """() => {
              const result = {};
              const visible = (el) => {
                const s = getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden') return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              };
              const attrs = (el) => [
                el.getAttribute('name') || '',
                el.getAttribute('id') || '',
                el.getAttribute('autocomplete') || '',
                el.getAttribute('placeholder') || '',
                el.getAttribute('aria-label') || '',
                el.getAttribute('type') || '',
              ].join(' ').toLowerCase();

              const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
              const password = inputs.find(el => (el.type || '').toLowerCase() === 'password');
              if (password) {
                if (password.id) result.password = '#' + CSS.escape(password.id);
                else if (password.name) result.password = 'input[type="password"][name="' + password.name + '"]';
                else result.password = 'input[type="password"]';
              }

              const user = inputs.find(el => {
                const t = (el.type || 'text').toLowerCase();
                if (!['text', 'email', 'tel'].includes(t)) return false;
                const a = attrs(el);
                return /email|user|login|account|identifier/.test(a) || t === 'email';
              }) || inputs.find(el => {
                const t = (el.type || 'text').toLowerCase();
                return ['text', 'email'].includes(t) && el !== password;
              });
              if (user) {
                if (user.id) result.username = '#' + CSS.escape(user.id);
                else if (user.name) result.username = 'input[name="' + user.name + '"]';
                else if ((user.type || '').toLowerCase() === 'email') result.username = 'input[type="email"]';
                else result.username = 'input[type="text"], input[type="email"]';
              }

              const submit =
                document.querySelector('button[type="submit"], input[type="submit"]') ||
                Array.from(document.querySelectorAll('button')).find(b =>
                  /sign\\s*in|log\\s*in|continue|submit/i.test(b.innerText || '')
                );
              if (submit) {
                if (submit.id) result.submit = '#' + CSS.escape(submit.id);
                else if (submit.getAttribute('type') === 'submit')
                  result.submit = 'button[type="submit"], input[type="submit"]';
                else result.submit = 'button';
              }
              return result;
            }"""
        )

    async def stop(self) -> None:
        """Stop tracing and close context/browser/Playwright without leaking resources."""
        try:
            if self._tracing and self.context:
                try:
                    if self.trace_path:
                        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
                        await self.context.tracing.stop(path=str(self.trace_path))
                        logger.info("Trace saved: %s", self.trace_path)
                except Exception as exc:
                    logger.warning("Failed to stop tracing: %s", exc)
                    try:
                        await self.context.tracing.stop()
                    except Exception:
                        pass
                finally:
                    self._tracing = False

            if self.context:
                try:
                    await self.context.close()
                except Exception as exc:
                    logger.warning("Context close error: %s", exc)

            if self.browser:
                try:
                    await self.browser.close()
                except Exception as exc:
                    logger.warning("Browser close error: %s", exc)

            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception as exc:
                    logger.warning("Playwright stop error: %s", exc)
        finally:
            self.page = None
            self.context = None
            self.browser = None
            self._playwright = None
            self._started = False
            logger.info("Browser stopped (run=%s)", self.run_id)

    def require_page(self) -> Page:
        if not self.page or self.page.is_closed():
            raise RuntimeError("Browser page is not available")
        if self._crashed:
            raise RuntimeError("Browser page has crashed")
        return self.page

"""Playwright driver adapter conforming to spec 0003 Slice 4.

Provides live browser execution via Playwright Chromium with:
1. Strict pre navigation URL and DNS address validation.
2. In flight route interception evaluating all requests, redirects, and subresources.
3. Explicit mitigation against DNS rebinding, Service Workers (service_workers="block"),
   and WebSockets (protocol blocking and init script disabling).
4. Graceful error degradation mapping timeouts to TIMED_OUT, security blocks and bot challenges
   to BLOCKED, and crashes to UNAVAILABLE.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from config.config import Settings
from contracts.browser import (
    BrowserExtractionPort,
    BrowserRenderResult,
    BrowserRenderStatus,
    BrowserResourceUsage,
    BrowserTaskPayload,
)
from functions.browser.network import (
    BrowserSecurityError,
    RouteSecurityInterceptor,
    SecureEgressProxy,
    validate_navigation_url,
)
from functions.browser.parser import parse_rendered_entities

logger = logging.getLogger(__name__)

# Default headless Chromium flags for serverless and containerized environments
DEFAULT_CHROMIUM_ARGS: list[str] = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--single-process",
    "--no-zygote",
]

# Signatures for bot detection and anti scraping challenges
_BOT_CHALLENGE_PATTERNS: tuple[str, ...] = (
    "cf-challenge",
    "cf-turnstile",
    "challenge-running",
    "just a moment...",
    "verify you are human",
    "attention required! | cloudflare",
    "please verify you are a human",
    "hcaptcha",
    "g-recaptcha",
)


def ensure_sparticuz_chromium(
    *,
    layer_dir: str | Path | None = None,
    target_path: str | Path = "/tmp/chromium",
) -> str | None:
    """Ensures Sparticuz Chromium executable is decompressed and ready for execution."""
    resolved_target = Path(target_path)
    if resolved_target.exists() and os.access(resolved_target, os.X_OK):
        return str(resolved_target)

    candidate_layer_dirs: list[Path] = []
    if layer_dir is not None:
        candidate_layer_dirs.append(Path(layer_dir))
    env_layer = os.environ.get("SPARTICUZ_LAYER_DIR")
    if env_layer:
        candidate_layer_dirs.append(Path(env_layer))
    candidate_layer_dirs.extend([
        Path("/opt/nodejs/node_modules/@sparticuz/chromium"),
        Path("/opt"),
    ])

    resolved_sparticuz_dir: Path | None = None
    for cand in candidate_layer_dirs:
        if cand.name == "@sparticuz" and (cand / "chromium").is_dir():
            resolved_sparticuz_dir = cand / "chromium"
            break
        if (cand / "nodejs" / "node_modules" / "@sparticuz" / "chromium").is_dir():
            resolved_sparticuz_dir = cand / "nodejs" / "node_modules" / "@sparticuz" / "chromium"
            break
        if (cand / "bin" / "chromium.br").is_file():
            resolved_sparticuz_dir = cand
            break

    if resolved_sparticuz_dir is None or not (resolved_sparticuz_dir / "bin" / "chromium.br").is_file():
        return None

    node_bin: str | Path | None = None
    try:
        import playwright

        driver_node = Path(playwright.__file__).parent / "driver" / "node"
        if driver_node.exists() and os.access(driver_node, os.X_OK):
            node_bin = driver_node
    except Exception:
        pass

    if node_bin is None:
        node_bin = shutil.which("node")

    if node_bin is None or not os.access(node_bin, os.X_OK):
        logger.warning(
            "Sparticuz Chromium layer detected but no executable Node runtime found for bootstrap decompression"
        )
        return None

    node_modules_dir = resolved_sparticuz_dir.parent.parent
    bootstrap_script = (
        "const chromium = require('@sparticuz/chromium');"
        "async function main() {"
        "  const p = await chromium.executablePath();"
        "  console.log(p);"
        "}"
        "main().catch(err => {"
        "  console.error(err);"
        "  process.exit(1);"
        "});"
    )

    env = os.environ.copy()
    existing_node_path = env.get("NODE_PATH", "")
    env["NODE_PATH"] = f"{node_modules_dir}:{existing_node_path}" if existing_node_path else str(node_modules_dir)
    if "AWS_EXECUTION_ENV" not in env:
        env["AWS_EXECUTION_ENV"] = "AWS_Lambda_nodejs20.x"

    try:
        res = subprocess.run(
            [str(node_bin), "-e", bootstrap_script],
            cwd=str(resolved_sparticuz_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if res.returncode == 0 and resolved_target.exists() and os.access(resolved_target, os.X_OK):
            al2023_lib = Path("/tmp/al2023/lib")
            if al2023_lib.exists():
                curr_ld = os.environ.get("LD_LIBRARY_PATH", "")
                if "/tmp/al2023/lib" not in curr_ld:
                    os.environ["LD_LIBRARY_PATH"] = f"/tmp/al2023/lib:{curr_ld}" if curr_ld else "/tmp/al2023/lib"
            fonts_dir = Path("/tmp/fonts")
            if fonts_dir.exists() and "FONTCONFIG_PATH" not in os.environ:
                os.environ["FONTCONFIG_PATH"] = "/tmp/fonts"
            return str(resolved_target)
        logger.warning(
            "Sparticuz Chromium bootstrap execution failed (code %s): STDOUT: %s, STDERR: %s",
            res.returncode,
            res.stdout.strip(),
            res.stderr.strip(),
        )
        return None
    except Exception as exc:
        logger.warning("Sparticuz Chromium bootstrap decompression failed: %s", exc)
        return None


class PlaywrightBrowserAdapter(BrowserExtractionPort):
    """Playwright extraction adapter executing dynamic page rendering with live network guards."""

    def __init__(
        self,
        *,
        browser: Browser | None = None,
        dns_resolver: Callable[[str, int], list[Any]] | None = None,
        ip_validator: Callable[[Any], bool] | None = None,
        chromium_path: str | None = None,
        launch_args: list[str] | None = None,
        headless: bool = True,
        egress_proxy: SecureEgressProxy | None = None,
        enable_egress_proxy: bool = True,
    ) -> None:
        self._injected_browser = browser
        self._dns_resolver = dns_resolver
        self._ip_validator = ip_validator
        self._chromium_path = chromium_path
        self._launch_args = launch_args or DEFAULT_CHROMIUM_ARGS
        self._headless = headless
        self._egress_proxy = egress_proxy
        self._enable_egress_proxy = enable_egress_proxy

    async def render_page(self, payload: BrowserTaskPayload) -> BrowserRenderResult:
        """Renders dynamic web page content under strict timeouts and network controls."""
        start_time = time.monotonic()

        # 1. Pre navigation URL and DNS address validation (AC-4)
        try:
            validate_navigation_url(
                payload.url,
                dns_resolver=self._dns_resolver,
                ip_validator=self._ip_validator,
            )
        except BrowserSecurityError as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning("Pre navigation security check rejected %s: %s", payload.url, exc)
            return BrowserRenderResult(
                status=BrowserRenderStatus.BLOCKED,
                canonical_url=payload.canonical_url,
                effective_url=payload.url,
                content_hash=None,
                excerpt_text=None,
                excerpt_locator=None,
                rendered_html_snippet=None,
                render_duration_ms=duration_ms,
                unavailable_reason=f"Pre navigation security policy blocked URL: {exc}",
                usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
            )

        # 2. Browser session execution
        if self._injected_browser is not None:
            return await self._render_with_browser(self._injected_browser, payload, start_time)

        # Launch dedicated browser instance
        playwright_cm = async_playwright()
        playwright = await playwright_cm.start()
        browser: Browser | None = None
        try:
            executable_path = self._chromium_path
            if not executable_path:
                try:
                    settings = Settings()
                    executable_path = settings.CHROMIUM_PATH
                except Exception:
                    executable_path = None

            if executable_path == "/tmp/chromium" or (executable_path and not os.path.exists(executable_path)):
                bootstrapped = ensure_sparticuz_chromium(target_path=executable_path or "/tmp/chromium")
                if bootstrapped:
                    executable_path = bootstrapped

            launch_kwargs: dict[str, Any] = {
                "headless": self._headless,
                "args": self._launch_args,
            }
            if executable_path:
                launch_kwargs["executable_path"] = executable_path

            browser = await playwright.chromium.launch(**launch_kwargs)
            return await self._render_with_browser(browser, payload, start_time)
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception as exc:
                    logger.debug("Error closing browser: %s", exc)
            try:
                await playwright.stop()
            except Exception as exc:
                logger.debug("Error stopping playwright: %s", exc)

    async def _render_with_browser(
        self,
        browser: Browser,
        payload: BrowserTaskPayload,
        start_time: float,
    ) -> BrowserRenderResult:
        """Executes rendering inside an isolated, guarded browser context."""
        context: BrowserContext | None = None
        page: Page | None = None
        background_tasks: set[asyncio.Task[Any]] = set()
        interceptor = RouteSecurityInterceptor(
            dns_resolver=self._dns_resolver,
            ip_validator=self._ip_validator,
        )
        proxy: SecureEgressProxy | None = None
        proxy_started_here = False
        if self._enable_egress_proxy:
            proxy = self._egress_proxy or SecureEgressProxy(
                dns_resolver=self._dns_resolver,
                ip_validator=self._ip_validator,
            )
            if proxy.server is None:
                await proxy.start()
                proxy_started_here = True

        try:
            # 1. Create context with Service Workers explicitly blocked and Egress Proxy configured
            context_kwargs: dict[str, Any] = {
                "service_workers": "block",
                "ignore_https_errors": False,
                "viewport": {"width": 1280, "height": 800},
            }
            if proxy is not None and proxy.port > 0:
                context_kwargs["proxy"] = {
                    "server": f"http://127.0.0.1:{proxy.port}",
                    "bypass": "<-loopback>",
                }

            context = await browser.new_context(**context_kwargs)

            # 2. Register in flight route interception on context
            await context.route("**/*", interceptor)

            # 3. Disable WebSockets and Service Worker API in page context
            await context.add_init_script(
                "delete window.WebSocket; "
                "window.WebSocket = function() { throw new Error('WebSockets are disabled'); }; "
                "try { Object.defineProperty(navigator, 'serviceWorker', { get: () => undefined, configurable: true }); } catch (e) {}"
            )

            page = await context.new_page()

            # Register response listener to validate HTTP redirect destinations (AC-4)
            def on_response(res: Any) -> None:
                if getattr(res, "status", 0) in (301, 302, 303, 307, 308):
                    task = asyncio.create_task(interceptor.check_response(res, page=page))
                    background_tasks.add(task)
                    task.add_done_callback(background_tasks.discard)

            page.on("response", on_response)

            timeout_ms = payload.timeout_seconds * 1000
            wait_until_strategy: Any = payload.wait_until.value

            # 4. Perform bounded navigation
            response = await page.goto(
                payload.url,
                timeout=timeout_ms,
                wait_until=wait_until_strategy,
            )

            effective_url = page.url or payload.url

            # 5. Check if route interception or egress proxy blocked the target or redirect hops
            all_blocked = interceptor.blocked_requests + (proxy.blocked_requests if proxy else [])
            if all_blocked:
                main_blocked = [
                    b
                    for b in all_blocked
                    if b["url"] == payload.url or b["url"] == effective_url
                    or payload.url.startswith(b["url"]) or effective_url.startswith(b["url"])
                ]
                if main_blocked:
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    reason = main_blocked[0]["reason"]
                    return BrowserRenderResult(
                        status=BrowserRenderStatus.BLOCKED,
                        canonical_url=payload.canonical_url,
                        effective_url=effective_url,
                        content_hash=None,
                        excerpt_text=None,
                        excerpt_locator=None,
                        rendered_html_snippet=None,
                        render_duration_ms=duration_ms,
                        unavailable_reason=f"Security policy blocked request: {reason}",
                        usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                        parsed_lodging_observations=[],
                        parsed_transit_observations=[],
                    )

            # 6. Check HTTP status code for bot challenge / forbidden
            if response is not None and response.status in (403, 429):
                duration_ms = int((time.monotonic() - start_time) * 1000)
                return BrowserRenderResult(
                    status=BrowserRenderStatus.BLOCKED,
                    canonical_url=payload.canonical_url,
                    effective_url=effective_url,
                    content_hash=None,
                    excerpt_text=None,
                    excerpt_locator=None,
                    rendered_html_snippet=None,
                    render_duration_ms=duration_ms,
                    unavailable_reason=f"HTTP {response.status} access denied or anti bot challenge",
                    usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                    parsed_lodging_observations=[],
                    parsed_transit_observations=[],
                )

            # 7. Check for bot verification indicators in page title
            page_title = (await page.title() or "").lower()
            for pattern in _BOT_CHALLENGE_PATTERNS:
                if pattern in page_title:
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    return BrowserRenderResult(
                        status=BrowserRenderStatus.BLOCKED,
                        canonical_url=payload.canonical_url,
                        effective_url=effective_url,
                        content_hash=None,
                        excerpt_text=None,
                        excerpt_locator=None,
                        rendered_html_snippet=None,
                        render_duration_ms=duration_ms,
                        unavailable_reason="Bot verification or anti bot challenge detected",
                        usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                        parsed_lodging_observations=[],
                        parsed_transit_observations=[],
                    )

            # 8. Optional selector waiting before capturing settled DOM snapshot (C5)
            if payload.selector_target:
                try:
                    await page.wait_for_selector(payload.selector_target, timeout=timeout_ms)
                except PlaywrightTimeoutError:
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    return BrowserRenderResult(
                        status=BrowserRenderStatus.TIMED_OUT,
                        canonical_url=payload.canonical_url,
                        effective_url=effective_url,
                        content_hash=None,
                        excerpt_text=None,
                        excerpt_locator=None,
                        rendered_html_snippet=None,
                        render_duration_ms=duration_ms,
                        unavailable_reason=f"Timeout waiting for selector target '{payload.selector_target}'",
                        usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                        parsed_lodging_observations=[],
                        parsed_transit_observations=[],
                    )

            # 9. Consistent post-wait DOM snapshot capture (C5)
            rendered_html = await page.content()
            html_lower = rendered_html.lower()

            for pattern in _BOT_CHALLENGE_PATTERNS:
                if pattern in html_lower[:5000]:
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    return BrowserRenderResult(
                        status=BrowserRenderStatus.BLOCKED,
                        canonical_url=payload.canonical_url,
                        effective_url=effective_url,
                        content_hash=None,
                        excerpt_text=None,
                        excerpt_locator=None,
                        rendered_html_snippet=None,
                        render_duration_ms=duration_ms,
                        unavailable_reason="Bot verification or anti bot challenge detected",
                        usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                        parsed_lodging_observations=[],
                        parsed_transit_observations=[],
                    )

            # 9. Extract substantive excerpt text and locator from settled DOM
            excerpt_text: str | None = None
            excerpt_locator: str | None = None

            if payload.selector_target:
                target_element = await page.query_selector(payload.selector_target)
                if target_element:
                    text_val = await target_element.inner_text()
                    if text_val and text_val.strip():
                        excerpt_text = " ".join(text_val.split())[:1000]
                        excerpt_locator = payload.selector_target

            if not excerpt_text:
                body_element = await page.query_selector("body")
                if body_element:
                    text_val = await body_element.inner_text()
                    if text_val and text_val.strip():
                        excerpt_text = " ".join(text_val.split())[:1000]
                        excerpt_locator = "body"

            if not excerpt_text or not excerpt_text.strip():
                duration_ms = int((time.monotonic() - start_time) * 1000)
                return BrowserRenderResult(
                    status=BrowserRenderStatus.UNAVAILABLE,
                    canonical_url=payload.canonical_url,
                    effective_url=effective_url,
                    content_hash=None,
                    excerpt_text=None,
                    excerpt_locator=None,
                    rendered_html_snippet=None,
                    render_duration_ms=duration_ms,
                    unavailable_reason="Rendered page contained no substantive text",
                    usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                    parsed_lodging_observations=[],
                    parsed_transit_observations=[],
                )

            # 10. Extract entities and assemble successful result strictly from post-wait snapshot
            content_hash = hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest()
            html_snippet = rendered_html[:50000] if rendered_html else None

            parsed_lodging = []
            parsed_transit = []
            try:
                raw_entities = parse_rendered_entities(
                    html_content=rendered_html,
                    category=payload.category,
                    canonical_url=payload.canonical_url,
                )
                category_lower = payload.category.lower().strip()
                if "lodging" in category_lower or "hotel" in category_lower:
                    from decimal import Decimal
                    from urllib.parse import urlparse

                    from contracts.evidence import HotelObservation
                    from contracts.money import Currency

                    domain = urlparse(payload.canonical_url).netloc or "unknown"
                    for item in raw_entities:
                        try:
                            obs = HotelObservation(
                                property_name=item["property_name"],
                                location_name=domain,
                                price_per_night=Decimal(str(item["price_per_night"])),
                                currency=Currency.INR,
                                rating=item.get("rating"),
                                provider=domain,
                                source_url=payload.canonical_url,
                                deep_link=payload.canonical_url,
                            )
                            parsed_lodging.append(obs)
                        except Exception:
                            pass
                elif "transit" in category_lower or "fare" in category_lower:
                    from contracts.evidence import FareObservation

                    for item in raw_entities:
                        try:
                            parsed_transit.append(FareObservation.model_validate(item))
                        except Exception:
                            pass
            except Exception as exc:
                logger.debug("Entity parsing encountered exception: %s", exc)

            duration_ms = int((time.monotonic() - start_time) * 1000)
            return BrowserRenderResult(
                status=BrowserRenderStatus.SUCCESS,
                canonical_url=payload.canonical_url,
                effective_url=effective_url,
                content_hash=content_hash,
                excerpt_text=excerpt_text,
                excerpt_locator=excerpt_locator,
                rendered_html_snippet=html_snippet,
                render_duration_ms=duration_ms,
                unavailable_reason=None,
                usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                parsed_lodging_observations=parsed_lodging,
                parsed_transit_observations=parsed_transit,
            )

        except PlaywrightTimeoutError:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            all_blocked = interceptor.blocked_requests + (proxy.blocked_requests if proxy else [])
            if all_blocked:
                reasons = "; ".join(b["reason"] for b in all_blocked)
                return BrowserRenderResult(
                    status=BrowserRenderStatus.BLOCKED,
                    canonical_url=payload.canonical_url,
                    effective_url=payload.url,
                    content_hash=None,
                    excerpt_text=None,
                    excerpt_locator=None,
                    rendered_html_snippet=None,
                    render_duration_ms=duration_ms,
                    unavailable_reason=f"Security policy blocked navigation: {reasons}",
                    usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                    parsed_lodging_observations=[],
                    parsed_transit_observations=[],
                )
            return BrowserRenderResult(
                status=BrowserRenderStatus.TIMED_OUT,
                canonical_url=payload.canonical_url,
                effective_url=payload.url,
                content_hash=None,
                excerpt_text=None,
                excerpt_locator=None,
                rendered_html_snippet=None,
                render_duration_ms=duration_ms,
                unavailable_reason=f"Navigation timeout of {payload.timeout_seconds} seconds exceeded",
                usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
            )
        except PlaywrightError as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            all_blocked = interceptor.blocked_requests + (proxy.blocked_requests if proxy else [])
            if all_blocked:
                reasons = "; ".join(b["reason"] for b in all_blocked)
                return BrowserRenderResult(
                    status=BrowserRenderStatus.BLOCKED,
                    canonical_url=payload.canonical_url,
                    effective_url=payload.url,
                    content_hash=None,
                    excerpt_text=None,
                    excerpt_locator=None,
                    rendered_html_snippet=None,
                    render_duration_ms=duration_ms,
                    unavailable_reason=f"Security policy blocked navigation: {reasons}",
                    usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                    parsed_lodging_observations=[],
                    parsed_transit_observations=[],
                )

            exc_str = str(exc)
            is_timeout = "timeout" in exc_str.lower()
            is_blocked = "blocked" in exc_str.lower() or "403" in exc_str

            status = (
                BrowserRenderStatus.TIMED_OUT
                if is_timeout
                else (
                    BrowserRenderStatus.BLOCKED if is_blocked else BrowserRenderStatus.UNAVAILABLE
                )
            )
            return BrowserRenderResult(
                status=status,
                canonical_url=payload.canonical_url,
                effective_url=payload.url,
                content_hash=None,
                excerpt_text=None,
                excerpt_locator=None,
                rendered_html_snippet=None,
                render_duration_ms=duration_ms,
                unavailable_reason=f"Browser navigation error: {exc}",
                usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            return BrowserRenderResult(
                status=BrowserRenderStatus.UNAVAILABLE,
                canonical_url=payload.canonical_url,
                effective_url=payload.url,
                content_hash=None,
                excerpt_text=None,
                excerpt_locator=None,
                rendered_html_snippet=None,
                render_duration_ms=duration_ms,
                unavailable_reason=f"Unexpected rendering error: {exc}",
                usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
            )
        finally:
            if background_tasks:
                for task in list(background_tasks):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*background_tasks, return_exceptions=True)
            if page is not None and hasattr(page, "is_closed") and not page.is_closed():
                try:
                    await page.close()
                except Exception as exc:
                    logger.debug("Error closing page: %s", exc)
            if context is not None:
                try:
                    await context.close()
                except Exception as exc:
                    logger.debug("Error closing context: %s", exc)
            if proxy_started_here and proxy is not None:
                try:
                    await proxy.stop()
                except Exception as exc:
                    logger.debug("Error stopping egress proxy: %s", exc)

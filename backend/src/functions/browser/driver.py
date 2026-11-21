"""Playwright driver adapter conforming to spec 0003 Slice 4.

Provides live browser execution via Playwright with standalone Obscura CDP server with:
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
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
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
    validate_navigation_url_async,
)
from functions.browser.parser import parse_rendered_entities

logger = logging.getLogger(__name__)

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


def resolve_obscura_binary(
    *,
    target_path: str | Path | None = None,
) -> str | None:
    """Resolves Obscura executable path across local, package, container, and temporary paths."""
    resolved_target = Path(target_path) if target_path else None
    if resolved_target and resolved_target.exists() and os.access(resolved_target, os.X_OK):
        return str(resolved_target)

    candidates: list[Path] = []
    if resolved_target is not None:
        candidates.append(resolved_target)

    env_path = os.environ.get("OBSCURA_PATH") or os.environ.get("CHROMIUM_PATH")
    if env_path:
        candidates.append(Path(env_path))

    try:
        settings = Settings()
        if settings.OBSCURA_PATH:
            candidates.append(Path(settings.OBSCURA_PATH))
    except Exception:
        pass

    curr_dir = Path(__file__).resolve().parent
    candidates.extend(
        [
            curr_dir / "bin" / "obscura",
            curr_dir.parents[2] / "build" / "browser" / "bin" / "obscura",
            curr_dir.parents[2] / "build" / "bin" / "obscura",
            curr_dir.parents[2] / "build" / "bin" / "al2023" / "obscura",
            curr_dir.parents[2] / "build" / "bin" / "arch" / "obscura",
            curr_dir.parents[2] / "build" / "browser" / "obscura",
            Path("/var/task/bin/obscura"),
            Path("/var/task/obscura"),
            Path("/opt/bin/obscura"),
            Path("/opt/obscura/obscura"),
            Path("/tmp/obscura-bin/obscura"),
            Path("/tmp/al2023-target/release-dist/obscura"),
            Path("/tmp/obscura"),
        ]
    )

    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand.resolve())

    which_obscura = shutil.which("obscura")
    if which_obscura and os.access(which_obscura, os.X_OK):
        return which_obscura

    return None


_ACTIVE_BROWSER_PIDS: set[int] = set()


def get_descendant_pids(root_pid: int) -> list[int]:
    """Finds all child and descendant PIDs of root_pid."""
    children_map: dict[int, list[int]] = {}
    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        for entry in proc_dir.iterdir():
            if entry.name.isdigit():
                try:
                    stat_content = (entry / "stat").read_text()
                    rparen = stat_content.rfind(")")
                    if rparen != -1:
                        fields = stat_content[rparen + 1 :].split()
                        ppid = int(fields[1])
                        pid = int(entry.name)
                        children_map.setdefault(ppid, []).append(pid)
                except Exception:
                    continue
    else:
        fallback_descendants: list[int] = []
        fallback_queue = [root_pid]
        while fallback_queue:
            curr = fallback_queue.pop(0)
            try:
                out = subprocess.check_output(["pgrep", "-P", str(curr)], stderr=subprocess.DEVNULL)
                for line in out.decode().splitlines():
                    if line.strip().isdigit():
                        child = int(line.strip())
                        fallback_descendants.append(child)
                        fallback_queue.append(child)
            except Exception:
                pass
        return fallback_descendants

    descendants: list[int] = []
    queue = [root_pid]
    while queue:
        curr = queue.pop(0)
        for child in children_map.get(curr, []):
            descendants.append(child)
            queue.append(child)
    return descendants


def is_pid_alive(pid: int) -> bool:
    """Returns True if the process exists and is actively running (not terminated or zombie)."""
    stat_file = Path(f"/proc/{pid}/stat")
    if not stat_file.exists():
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    try:
        content = stat_file.read_text()
        rparen = content.rfind(")")
        if rparen != -1:
            state = content[rparen + 1 :].split()[0]
            if state in ("Z", "X"):
                return False
        return True
    except Exception:
        return False


def force_kill_process_tree(root_pid: int) -> None:
    """Terminates root_pid and all its descendant processes via SIGKILL and reaps zombies."""
    descendants = get_descendant_pids(root_pid)
    all_pids = descendants + [root_pid]
    for pid in all_pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception as exc:
            logger.debug("Error killing process %d: %s", pid, exc)

    # Allow kernel brief interval to deschedule and reap terminated processes
    for _ in range(10):
        for pid in all_pids:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, ProcessLookupError, PermissionError):
                pass
        if not any(is_pid_alive(p) for p in all_pids):
            break
        time.sleep(0.005)


def terminate_all_browser_processes() -> None:
    """Forcibly terminates any tracked Playwright driver and Obscura processes."""
    for pid in list(_ACTIVE_BROWSER_PIDS):
        force_kill_process_tree(pid)
    _ACTIVE_BROWSER_PIDS.clear()


class PlaywrightBrowserAdapter(BrowserExtractionPort):
    """Playwright extraction adapter executing dynamic page rendering with live network guards."""

    def __init__(
        self,
        *,
        browser: Browser | None = None,
        dns_resolver: Callable[[str, int], Any] | None = None,
        ip_validator: Callable[[Any], bool] | None = None,
        obscura_path: str | None = None,
        chromium_path: str | None = None,
        headless: bool = True,
        egress_proxy: SecureEgressProxy | None = None,
        enable_egress_proxy: bool = True,
        allow_private_network: bool = False,
        dns_timeout: float = 2.0,
        dns_nameservers: Sequence[str] | None = None,
        dns_port: int = 53,
        startup_timeout: float = 15.0,
    ) -> None:
        self._injected_browser = browser
        self._dns_resolver = dns_resolver
        self._ip_validator = ip_validator
        self._obscura_path = obscura_path or chromium_path
        self._headless = headless
        self._egress_proxy = egress_proxy
        self._enable_egress_proxy = enable_egress_proxy
        self._allow_private_network = allow_private_network
        self._dns_timeout = dns_timeout
        self._dns_nameservers = dns_nameservers
        self._dns_port = dns_port
        self._startup_timeout = startup_timeout
        self._active_driver_pids: set[int] = set()

    def terminate_residual_processes(self) -> None:
        """Terminates any tracked Playwright or Obscura processes remaining from abandoned operations."""
        for pid in list(self._active_driver_pids):
            force_kill_process_tree(pid)
            _ACTIVE_BROWSER_PIDS.discard(pid)
        self._active_driver_pids.clear()

    async def render_page(self, payload: BrowserTaskPayload) -> BrowserRenderResult:
        """Renders dynamic web page content under strict timeouts and network controls."""
        start_time = time.monotonic()

        # 1. Pre navigation URL and DNS address validation (AC-4, bounded async DNS without executor threads)
        try:
            await validate_navigation_url_async(
                payload.url,
                timeout=self._dns_timeout,
                dns_resolver=self._dns_resolver,
                ip_validator=self._ip_validator,
                dns_nameservers=self._dns_nameservers,
                dns_port=self._dns_port,
            )
        except TimeoutError as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning("Pre navigation DNS resolution timed out for %s: %s", payload.url, exc)
            return BrowserRenderResult(
                status=BrowserRenderStatus.TIMED_OUT,
                canonical_url=payload.canonical_url,
                effective_url=payload.url,
                content_hash=None,
                excerpt_text=None,
                excerpt_locator=None,
                rendered_html_snippet=None,
                render_duration_ms=duration_ms,
                unavailable_reason=f"Pre navigation DNS resolution timed out: {exc}",
                usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
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

        proxy: SecureEgressProxy | None = None
        proxy_started_here = False
        if self._enable_egress_proxy:
            proxy = self._egress_proxy or SecureEgressProxy(
                dns_resolver=self._dns_resolver,
                ip_validator=self._ip_validator,
                dns_timeout=self._dns_timeout,
                dns_nameservers=self._dns_nameservers,
                dns_port=self._dns_port,
            )
            if proxy.server is None:
                await proxy.start()
                proxy_started_here = True

        executable_path = resolve_obscura_binary(target_path=self._obscura_path)
        if not executable_path:
            if proxy_started_here and proxy is not None:
                await proxy.stop()
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
                unavailable_reason="Obscura browser binary not found; checked OBSCURA_PATH and standard locations",
                usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
            )

        ready_file = Path(f"/tmp/obscura_ready_{uuid.uuid4().hex}.json")
        stderr_file = tempfile.NamedTemporaryFile(
            prefix="obscura_stderr_",
            suffix=".log",
            delete=False,
        )
        stderr_file_path = Path(stderr_file.name)
        obscura_proc: subprocess.Popen[bytes] | None = None
        worker_pid: int | None = None
        driver_pid: int | None = None
        browser: Browser | None = None
        playwright_cm = async_playwright()
        playwright = None

        try:
            cmd = [
                executable_path,
                "serve",
                "--port",
                "0",
                "--host",
                "127.0.0.1",
                "--ready-file",
                str(ready_file),
                "--quiet",
            ]
            if proxy is not None and proxy.port > 0:
                cmd.extend(["--proxy", f"http://127.0.0.1:{proxy.port}"])
            if self._allow_private_network:
                cmd.append("--allow-private-network")

            env = os.environ.copy()
            obscura_dir = str(Path(executable_path).parent)
            curr_path = env.get("PATH", "")
            env["PATH"] = f"{obscura_dir}:{curr_path}" if curr_path else obscura_dir

            obscura_proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
            )
            self._active_driver_pids.add(obscura_proc.pid)
            _ACTIVE_BROWSER_PIDS.add(obscura_proc.pid)

            ready_data: dict[str, Any] | None = None
            wait_deadline = time.monotonic() + self._startup_timeout
            while time.monotonic() < wait_deadline:
                if obscura_proc.poll() is not None:
                    try:
                        stderr_txt = stderr_file_path.read_text(
                            encoding="utf-8",
                            errors="replace",
                        )[:1000]
                    except Exception:
                        stderr_txt = ""
                    raise RuntimeError(
                        f"Obscura process exited prematurely with code {obscura_proc.returncode}: {stderr_txt.strip()}"
                    )
                if ready_file.exists() and ready_file.stat().st_size > 0:
                    try:
                        content = ready_file.read_text(encoding="utf-8")
                        parsed = json.loads(content)
                        if isinstance(parsed, dict) and "port" in parsed:
                            ready_data = parsed
                            break
                    except Exception:
                        pass
                await asyncio.sleep(0.05)

            if not ready_data:
                raise TimeoutError("Obscura startup timed out waiting for ready file publication")

            if ready_data.get("host") != "127.0.0.1":
                raise BrowserSecurityError(
                    f"Obscura published non-loopback host: {ready_data.get('host')}"
                )

            cdp_port = int(ready_data["port"])
            worker_pid = ready_data.get("pid")
            if worker_pid and isinstance(worker_pid, int):
                self._active_driver_pids.add(worker_pid)
                _ACTIVE_BROWSER_PIDS.add(worker_pid)

            playwright = await playwright_cm.start()
            try:
                impl_obj = getattr(playwright, "_impl_obj", None)
                conn = getattr(impl_obj, "_connection", None)
                transport = getattr(conn, "_transport", None)
                proc = getattr(transport, "_proc", None)
                if proc and hasattr(proc, "pid") and isinstance(proc.pid, int):
                    driver_pid = proc.pid
                    self._active_driver_pids.add(driver_pid)
                    _ACTIVE_BROWSER_PIDS.add(driver_pid)
            except Exception as exc:
                logger.debug("Could not determine driver PID: %s", exc)

            browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            return await self._render_with_browser(
                browser,
                payload,
                start_time,
                proxy=proxy,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning("Obscura startup or connection timed out for %s: %s", payload.url, exc)
            return BrowserRenderResult(
                status=BrowserRenderStatus.TIMED_OUT,
                canonical_url=payload.canonical_url,
                effective_url=payload.url,
                content_hash=None,
                excerpt_text=None,
                excerpt_locator=None,
                rendered_html_snippet=None,
                render_duration_ms=duration_ms,
                unavailable_reason=f"Obscura startup or connection timed out: {exc}",
                usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
            )
        except BrowserSecurityError as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning("Obscura security policy rejected %s: %s", payload.url, exc)
            return BrowserRenderResult(
                status=BrowserRenderStatus.BLOCKED,
                canonical_url=payload.canonical_url,
                effective_url=payload.url,
                content_hash=None,
                excerpt_text=None,
                excerpt_locator=None,
                rendered_html_snippet=None,
                render_duration_ms=duration_ms,
                unavailable_reason=f"Security policy blocked URL: {exc}",
                usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.warning("Obscura process startup or connection error for %s: %s", payload.url, exc)
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
                unavailable_reason=f"Obscura process startup or connection error: {exc}",
                usage=BrowserResourceUsage(duration_ms=duration_ms, memory_mb=None),
                parsed_lodging_observations=[],
                parsed_transit_observations=[],
            )
        finally:
            if browser is not None:
                try:
                    await asyncio.wait_for(browser.close(), timeout=1.0)
                except Exception as exc:
                    logger.debug("Error closing browser: %s", exc)
            if playwright is not None:
                try:
                    await asyncio.wait_for(playwright.stop(), timeout=1.0)
                except Exception as exc:
                    logger.debug("Error stopping playwright: %s", exc)
            if driver_pid is not None:
                force_kill_process_tree(driver_pid)
                self._active_driver_pids.discard(driver_pid)
                _ACTIVE_BROWSER_PIDS.discard(driver_pid)
            if worker_pid is not None:
                force_kill_process_tree(worker_pid)
                self._active_driver_pids.discard(worker_pid)
                _ACTIVE_BROWSER_PIDS.discard(worker_pid)
            if obscura_proc is not None:
                force_kill_process_tree(obscura_proc.pid)
                self._active_driver_pids.discard(obscura_proc.pid)
                _ACTIVE_BROWSER_PIDS.discard(obscura_proc.pid)
            if stderr_file is not None:
                try:
                    stderr_file.close()
                except Exception:
                    pass
            if stderr_file_path is not None and stderr_file_path.exists():
                try:
                    stderr_file_path.unlink()
                except OSError:
                    pass
            if ready_file.exists():
                try:
                    ready_file.unlink(missing_ok=True)
                except OSError:
                    pass
            if proxy_started_here and proxy is not None:
                try:
                    await asyncio.wait_for(proxy.stop(), timeout=1.0)
                except Exception as exc:
                    logger.debug("Error stopping egress proxy: %s", exc)

    async def _render_with_browser(
        self,
        browser: Browser,
        payload: BrowserTaskPayload,
        start_time: float,
        proxy: SecureEgressProxy | None = None,
    ) -> BrowserRenderResult:
        """Executes rendering inside an isolated, guarded browser context."""
        context: BrowserContext | None = None
        page: Page | None = None
        background_tasks: set[asyncio.Task[Any]] = set()
        interceptor = RouteSecurityInterceptor(
            dns_resolver=self._dns_resolver,
            ip_validator=self._ip_validator,
            dns_timeout=self._dns_timeout,
            dns_nameservers=self._dns_nameservers,
            dns_port=self._dns_port,
        )
        local_proxy_started = False
        if proxy is None and self._enable_egress_proxy:
            proxy = self._egress_proxy or SecureEgressProxy(
                dns_resolver=self._dns_resolver,
                ip_validator=self._ip_validator,
                dns_timeout=self._dns_timeout,
                dns_nameservers=self._dns_nameservers,
                dns_port=self._dns_port,
            )
            if proxy.server is None:
                await proxy.start()
                local_proxy_started = True

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
                    if b["url"] == payload.url
                    or b["url"] == effective_url
                    or payload.url.startswith(b["url"])
                    or effective_url.startswith(b["url"])
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
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*background_tasks, return_exceptions=True),
                        timeout=1.0,
                    )
                except Exception as exc:
                    logger.debug("Error awaiting background tasks: %s", exc)
            if page is not None and hasattr(page, "is_closed") and not page.is_closed():
                try:
                    await asyncio.wait_for(page.close(), timeout=1.0)
                except Exception as exc:
                    logger.debug("Error closing page: %s", exc)
            if context is not None:
                try:
                    await asyncio.wait_for(context.close(), timeout=1.0)
                except Exception as exc:
                    logger.debug("Error closing context: %s", exc)
            if local_proxy_started and proxy is not None:
                try:
                    await asyncio.wait_for(proxy.stop(), timeout=1.0)
                except Exception as exc:
                    logger.debug("Error stopping egress proxy: %s", exc)

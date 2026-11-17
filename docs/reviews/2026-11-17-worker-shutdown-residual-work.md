# Review: Worker Shutdown Residual Work Isolation (2026-11-17)

**Reviewed by**: Senior Staff Engineer Reviewer
**Scope**: 7 files, commit 9735543
**Verdict**: Approve with nits

## Summary

This review evaluates commit 9735543 on specification 0007 Slice 8, titled "feat(workers): isolate residual work and terminate browser processes on handler exit (0007 Slice 8)". The change addresses residual work leakage across warm container invocations in AWS Lambda. It introduces proactive process tree discovery and SIGKILL termination for Playwright driver and Chromium processes, non blocking thread pool executor shut down, strict fail closed DNS timeout bounding in browser network interceptors, and two subprocess integration tests verifying state and process isolation across successive invocations in the same process.

The implementation is high quality, surgically targeted, and directly addresses the physical reality of warm container reuse in serverless environments. Static checks, linting, packaging integrity tests, and all fifteen integration tests pass with zero regressions.

The affected files reviewed are:
* [AGENTS.md](file:///home/swyra/projects/travel-agent/backend/AGENTS.md)
* [driver.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/driver.py#L196-L410)
* [network.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/network.py#L181-L473)
* [browser/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/consumer.py#L383-L416)
* [evidence_collector/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/consumer.py#L384-L413)
* [test_worker_deadline_and_partial_batch.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L1079-L1661)
* [verify.md](file:///home/swyra/projects/travel-agent/docs/specs/0007-trip-planner-e2e/verify.md#L28)

## Evaluation of Review Criteria

### 1. Residual Work Isolation and Event Loop Lifecycle
In serverless environments such as AWS Lambda, a container process is reused across multiple sequential invocations. If an invocation runs out of time or abandons a stubborn task, residual work must not leak into future invocations:
* In both [browser/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/consumer.py#L383-L416) and [evidence_collector/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/evidence_collector/consumer.py#L384-L413), each invocation operates inside an isolated event loop created via `asyncio.new_event_loop()`.
* On handler completion, cancellation, or time out, `loop.close()` destroys all pending callbacks and handles on the loop, and `asyncio.set_event_loop(None)` unbinds the thread loop.
* Because the event loop is closed and detached, background coroutines from an earlier invocation can never wake up or execute callbacks during a subsequent invocation in the same container.

### 2. Thread Pool Executor Bounding
Previously, consumers attempted to shut down the default thread pool executor via `loop.shutdown_default_executor()`. In Python asyncio, that method delegates to `ThreadPoolExecutor.shutdown(wait=True)`. If a worker thread is blocked on a synchronous call (such as a DNS resolution or system sleep), the handler would block until the thread finished, risking hard container termination:
* Commit 9735543 replaces `loop.shutdown_default_executor()` with explicit direct invocation: `executor.shutdown(wait=False, cancel_futures=True)`.
* Setting `cancel_futures=True` ensures that any queued work items that have not yet started are cancelled immediately without executing.
* Setting `wait=False` guarantees that the handler exit never blocks waiting for stubborn or long running threads.
* When a thread eventually finishes, its target future is detached from the closed event loop, leaving subsequent invocations fully isolated.

### 3. DNS Timeout Bounding and Fail Closed Security
Network operations in browser extraction rely on hostname validation and resolution to prevent server side request forgery. However, operating system `getaddrinfo` calls run synchronously in background threads and can block for tens of seconds if a target nameserver is unresponsive:
* In [network.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/network.py#L251-L269), `RouteSecurityInterceptor` wraps `resolve_and_validate_host` inside `asyncio.wait_for` with a configurable `dns_timeout` defaulting to 2.0 seconds.
* If resolution exceeds 2.0 seconds, `TimeoutError` is caught, a descriptive warning is logged, the target URL is appended to `self.blocked_requests`, and the navigation route is aborted with `await route.abort("blockedbyclient")`.
* In [network.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/network.py#L451-L473), `SecureEgressProxy` applies the same 2.0 second bound. If DNS resolution times out, it appends the target to `blocked_requests` and returns a clean `504 Gateway Timeout` HTTP response before closing the client socket.
* The security posture is strictly fail closed: unresolvable or slow domains are never permitted to bypass inspection.

### 4. Playwright and Chromium Process Tracking and SIGKILL Termination
Orphaned browser processes represent a serious hazard in containerized environments. If a page navigation times out or cleanup is abandoned, residual Node driver and Chromium processes remain resident, consuming hundreds of megabytes of RAM and leading to container exhaustion:
* In [driver.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/driver.py#L196-L285), the module maintains a global registry `_ACTIVE_BROWSER_PIDS` as well as adapter level tracking `self._active_driver_pids`.
* When `async_playwright().start()` initializes, the internal pipe transport subprocess PID is extracted defensively via `playwright._impl_obj._connection._transport._proc.pid` and registered.
* The `get_descendant_pids` utility scans `/proc` to inspect parent child relationships (`ppid`) across all active processes, correctly identifying the complete tree of descendant Chromium browser, zygote, and renderer processes.
* The `force_kill_process_tree` function issues `signal.SIGKILL` across all descendant PIDs and the root driver PID, followed by non blocking `os.waitpid(pid, os.WNOHANG)` reaping loops to eliminate zombie entries.
* In [browser/consumer.py](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/consumer.py#L402-L411), the handler `finally` block invokes `adapter.terminate_residual_processes()` and `terminate_all_browser_processes()`, guaranteeing that all tracked child processes are killed before the handler returns to AWS Lambda.

### 5. Fail Closed Budget Accounting and Lease Fencing
The interaction between task abandonment and financial budget reservations remains completely fail closed:
* If a worker times out or is abandoned, active reservations in `executions.budget['reservations']` remain in status `ACTIVE`.
* Expired worker leases prevent abandoned workers from writing results to the database upon late completion.
* Background recovery sweeps via `TaskRecoveryService.recover_abandoned_budget_reservations` transition expired reservations to `ABANDONED_UNCERTAIN`.
* Uncertain spend is added to `spent_cost_minor` and `uncertain_cost_minor`, subtracting from remaining balance so that retries cannot exceed user specified budget caps.

### 6. Successive Invocations in the Same Process
The test suite in [test_worker_deadline_and_partial_batch.py](file:///home/swyra/projects/travel-agent/backend/tests/integration/test_worker_deadline_and_partial_batch.py#L1079-L1661) introduces `run_two_successive_invocations_in_subprocess`:
* This helper executes two real sequential invocations of `lambda_handler` in the exact same OS child process under simulated Lambda context deadlines.
* In `test_lambda_handler_two_successive_invocations_same_process_evidence_residual_thread_isolation`, invocation 1 starts an infinite stubborn thread and times out. Invocation 2 executes in the same process, succeeds cleanly within 1.0 second, and confirms that database state and execution flow remain uncorrupted.
* In `test_lambda_handler_two_successive_invocations_same_process_browser_residual_process_cleanup`, invocation 1 launches Playwright and Chromium and suppresses cancellation. The handler tears down and issues SIGKILL. The test inspects `/proc` and confirms that both the driver PID and all descendant Chromium PIDs are dead (`alive_driver is False`, `alive_descendants == []`). Invocation 2 then executes a fast render in the same process and completes successfully.

## Findings

No blockers or major defects were identified. Two minor nits are noted for author consideration.

### Nits

* ⚪ [driver.py:270](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/driver.py#L270), `force_kill_process_tree` executes `time.sleep(0.02)` at the beginning of each loop iteration. On clean normal completions where browser and driver already stopped, this introduces an unconditional 20 millisecond delay. Moving the `if not any(is_pid_alive(p) for p in all_pids): break` check before the sleep would eliminate this latency on successful executions.
* ⚪ [driver.py:218](file:///home/swyra/projects/travel-agent/backend/src/functions/browser/driver.py#L218), `get_descendant_pids` falls back to `pgrep -P str(root_pid)` when `/proc` does not exist (such as on macOS developer workstations). The fallback inspects direct child processes only rather than recursing down the process tree for child processes discovered in the queue. On Linux and AWS Lambda, `/proc` is always present, so production behavior is unaffected.

## Strengths

* Robust multi level process cleanup: scanning `/proc` for parent process relationships ensures that both the Playwright Node runner and all Chromium child processes are terminated with SIGKILL, preventing container memory leaks.
* Non blocking thread pool tear down: shutting down executors with `wait=False` and `cancel_futures=True` ensures handlers never freeze on stubborn threads when Lambda usable time expires.
* Rigorous integration testing: executing successive invocations in the same OS child process directly proves that container reuse does not cause state leakage or resource starvation.
* Defensive DNS protection: bounding resolution to 2.0 seconds prevents slow upstream nameservers from exhausting the Lambda invocation budget.

## Test Verification Results

All tests pass cleanly in the local environment:
* `uv run pytest tests/integration/test_worker_deadline_and_partial_batch.py -v`: 15 passed in 29.52 seconds.
* `uv run ruff check src/functions/browser src/functions/evidence_collector tests/integration/test_worker_deadline_and_partial_batch.py`: all checks passed.
* `uv run mypy src/functions/browser/driver.py src/functions/browser/network.py src/functions/browser/consumer.py src/functions/evidence_collector/consumer.py`: clean with zero issues.
* `uv run pytest tests/unit/test_sam_package_size.py -k test_verify_handler_import_clean_lambda_environment && uv run pytest tests/unit/test_browser_packaging.py -k test_verify_browser_handler_import_clean_lambda_environment`: clean Lambda import isolation verified.

## Scope Update

The change satisfies the verification criteria for specification 0007 Slice 8. The documentation in [verify.md](file:///home/swyra/projects/travel-agent/docs/specs/0007-trip-planner-e2e/verify.md#L28) and [AGENTS.md](file:///home/swyra/projects/travel-agent/backend/AGENTS.md) accurately reflects the bounded shutdown and residual work isolation guarantees.

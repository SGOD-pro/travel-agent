# Review, main, 2026-10-16

**Reviewed by**: Staff Reviewer (inline inspection on main, author on Gemini)
**Scope**: 7 files, uncommitted (Spec 0003 Slice 5: SQS Consumer Handler, SAM Template, and Combined Packaging Acceptance)
**Verdict**: Approve (historical; see subsequent closure reconciliation below)

## Summary
Spec 0003 Slice 5 implements the dedicated SQS consumer Lambda handler, AWS SAM serverless template with external Chromium layer binding, and standalone packaging acceptance verification, satisfying acceptance criterion AC-8. The implementation enforces partial batch failure reporting via `ReportBatchItemFailures`, safely discards benign fencing claim conflicts, guards against Lambda execution timeouts by failing records early when remaining time is under 5000ms, and measures unpacked and compressed sizes against AWS Lambda limits. The unpacked function package measures 69.10 MB (well under the 80 MB function budget), compresses to 20.60 MB (well under the 50 MB limit and beneath the 35 MB target), and combines with a 130 MB Chromium layer to 199.10 MB (well under the 210 MB target and 250 MB AWS Lambda ceiling). All 18 Slice 5 unit tests, all 165 browser tests, and all 314 backend unit tests pass cleanly with zero lint and type errors.

## Strengths
- **Safe Batch Failure Protocol**: Conforms to standard AWS Lambda SQS partial batch failure semantics (`ReportBatchItemFailures`), reporting only failed item identifiers while allowing successful items to be deleted by SQS.
- **Benign Fencing Conflict Discarding**: Safely discards `TaskClaimConflictError`, `TaskLeaseActiveError`, `TaskAlreadyTerminalError`, and `StaleQueueMessageError` without redriving to the dead letter queue, preventing poison pill looping on concurrent deliveries.
- **Proactive Execution Timeout Guard**: Inspects `context.get_remaining_time_in_millis()` and flags messages as deadline failures if remaining time is under 5000ms, ensuring clean SQS redrives instead of abrupt Lambda timeouts.
- **Strict Binary Decoupling**: Unlinks bundled Node.js binaries from the function artifact so that headless Chromium and Node runtime binaries reside exclusively in the 130 MB external Chromium layer in `/opt`.
- **Hermetic Isolation Verification**: Verifies clean import and execution under isolated Python 3.12 with `-S -P`, `PYTHONNOUSERSITE=1`, zero host site packages, and virtualenv paths stripped, asserting all forbidden modules raise `ModuleNotFoundError`.
- **Fast Rebuild Caching**: Uses SHA256 hashing of `requirements.txt` to cache dependency installation, allowing cached rebuilds to complete in under 1 second.
- **Zero Cross Worker Coupling**: Staged artifact includes only browser code, permitted 0001 execution support, configuration, and domain contracts, strictly excluding optimizer, API, evidence collector, and orchestrator graph modules.

## Test coverage
`TESTS = configured` (pytest). All 18 tests for Slice 5 and all 165 browser tests pass cleanly:
- Consumer unit suite (`test_browser_consumer.py`, 12 passed): verifies record parsing, non browser worker filtering, non render action filtering, benign fencing claim error discarding, execution engine error re-raising, zero failures on batch success, deadline remaining time (<5000ms) failures, unexpected record exceptions reported in batchItemFailures, and lambda_handler execution.
- Packaging unit suite (`test_browser_packaging.py`, 6 passed): verifies SAM template structure and parameters (MemorySize 1536MB, timeout 60s, DLQ redrive, layer binding), requirements dependency inclusion and exclusion, actual artifact building and size limits (unpacked function <= 80 MB, compressed ZIP <= 50 MB, combined <= 210 MB / 250 MB, layer constant 130 MB), clean environment import under `-S -P`, cached rebuild speed, and missing Python 3.12 error raising.
- Full browser suite: 165 tests passing across contracts, fixtures, orchestrator routing, worker fencing, Scrapling parsing, live Chromium execution, consumer, and packaging.
- Full backend regression suite: 314 tests passing with zero regressions.
- Static typing (`mypy`) and linting (`ruff`): 0 errors and 0 warnings across all browser package files and tests.


## 0003 closure reconciliation — 2026-10-16

Inspected repository HEAD: `35d9e60c64d4d4143b178eb5affd73dea846eb10`. This checkpoint reconciles the previous Approve reports with concrete source evidence. It does not implement fixes, rerun the project test suites, or establish final closure. Earlier Approve verdicts and Accepted/done markers are historical workflow records; they do not resolve the findings below.

| ID | Status | Finding and evidence | Required closure evidence |
|---|---|---|---|
| C1 | OPEN | `packaging.py` removes `playwright/driver/node`. No replacement driver executable is configured in the inspected driver/template. The clean-environment verifier imports handlers; it does not start Playwright. | Retain the required runtime or explicitly package/configure a compatible replacement. Start Playwright and render a controlled page from the isolated final artifact with the selected browser runtime. |
| C2 | OPEN | `CHROMIUM_LAYER_UNCOMPRESSED_BYTES = 130 * 1024 * 1024` is added to measured function bytes. The combined ~199 MB figure therefore includes an estimated layer, not a measured selected layer. | Identify and hash the actual function and layer artifacts, measure their extracted bytes, and prove compatible startup/rendering. Keep unavailable target-runtime proof explicitly deferred, never passed by an estimate. No AWS deployment is required for this closure work. |
| C3 | OPEN | `network.py` validates DNS addresses and then calls `route.continue_()`. Chromium's subsequent resolution/connection is not bound to those validated addresses. Repeated DNS checks and mocked rebinding tests do not prove connection-level protection. | Enforce the validated destination at connection/egress level and test the validation-to-connection change against controlled endpoints without contacting real metadata. |
| C4 | DISPROVED; no fix required | SAM `CodeUri: ../../../build/browser`, relative to `backend/src/functions/browser/template.yaml`, resolves to `backend/build/browser`, matching the builder default. The claimed `backend/src/build/browser` resolution is incorrect. | Independently executed Python pathlib normalization confirmed both paths match. This proves path arithmetic only, not SAM build/runtime success. |
| C5 | OPEN | `driver.py` captures `rendered_html = await page.content()` before `wait_for_selector()`; later entity parsing and stored HTML use that earlier capture. | Capture HTML after selector waiting and derive HTML, excerpt and parsed entities from the same post-wait snapshot. Add a delayed-content regression. |

### Evidence boundaries

- Independently performed here: read-only source inspection at the pinned HEAD and executed Python pathlib normalization for C4.
- Reported by existing documents, not independently rerun here: 167 browser tests, 314 backend unit tests, live Chromium and PostgreSQL checks, lint/type checks, function artifact sizes, and prior Approve verdicts. Counts vary between historical reports (165/167); preserve their attribution rather than treating them as a fresh consolidated run.
- No independently executed browser startup, packaging build, actual layer measurement, application test suite, or AWS deployment is claimed at this checkpoint.
- Prior Slice 4 claims of preventing DNS rebinding and Slice 5 claims of measured combined packaging/runtime acceptance are narrowed by C1–C3.

### Next action and ordering

Fix C1, C2, C3 and C5 within existing 0003, one bounded closure slice. Do not alter the correct CodeUri based on C4. Then run `/check verify → /check review → /sync`, recording exact revision, commands, artifacts, results and deferrals. Do not run roadmap sync concurrently with active implementation.

After closure, architect remote LAYA served from Kaggle through Cloudflare: configurable endpoint/auth, typed request/decision contracts, bounded timeouts/retries, deterministic policy validation and fallback. Local ONNX packaging is not the target.

Agreed order: remote LAYA → route optimizer → route API/map E2E → hosted LLM/trip planner E2E → connected live-scenario evaluation → media extractor/analyzer/location → composition/hardening. Browser remains evidence-rendering fallback; booking/payments remain future scope. No new milestone for these fixes and no AWS deployment now.

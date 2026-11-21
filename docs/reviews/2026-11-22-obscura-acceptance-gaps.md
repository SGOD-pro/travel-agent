# Review, main, 2026-11-22

**Reviewed by**: Staff Reviewer (subagent) (author on Gemini 2.5 Pro)
**Scope**: 8 files, uncommitted
**Verdict**: Approve with nits

## Summary
This review evaluates the acceptance corrections and hardening for Obscura browser worker integration. The changes successfully eliminate false positive verification risks by strictly confining browser executables to packaged artifacts, removing host PATH fallback, and introducing full production orchestration end to end testing with live graph checkpoint resumption. All 197 tests pass cleanly, static typing and linting are spotless, and process tree resource sampling accurately profiles memory usage across execution phases without unsupported performance assertions.

## Minor
### 🟡 Path prefix validation in Python fallback check, `backend/src/functions/browser/packaging.py:508`
**Problem**: In `verify_browser_startup_and_render_isolated_environment`, the defensive fallback for Python versions lacking `is_relative_to` checks `str(override_resolved).startswith(str(artifact_dir_resolved))`.
**Why it matters**: A raw string prefix check without directory separator boundaries or parent relationship verification allows false matches if an override path starts with the artifact directory name followed by other characters (for example `/tmp/artifact_other/obscura` matching prefix `/tmp/artifact`). While the project enforces Python 3.12 where `is_relative_to` is always available, the fallback code contains an unsafe path traversal boundary bug.
**Suggested fix**: Ensure the fallback checks parent path containment or directory separator boundaries, for example checking whether `artifact_dir_resolved` is equal to `override_resolved` or in `override_resolved.parents`.

### 🟡 Inconsistent candidate binary discovery paths, `backend/src/functions/browser/packaging.py:299`
**Problem**: `resolve_obscura_binary` in `driver.py` was extended to search `build/bin/al2023/obscura` and `build/bin/arch/obscura`, but `build_isolated_browser_artifact` in `packaging.py` only searches `build/bin/obscura` and omits the target specific binary subdirectories.
**Why it matters**: If `build/bin/obscura` is deleted or not copied while the target build output in `build/bin/al2023/obscura` exists, `packaging.py` fails to discover the staged binary and attempts fallback to host PATH rather than discovering the local AL2023 build output.
**Suggested fix**: Align the candidate list in `build_isolated_browser_artifact` with `resolve_obscura_binary` by adding `build/bin/al2023/obscura` and `build/bin/arch/obscura`.

### 🟡 Documentation discrepancy in Arch Linux cargo build command, `docs/specs/0003-browser-escalation/verify.md:185`
**Problem**: The reproduction snippet for Arch Linux in `verify.md` specifies `cargo build --release` with `--target-dir /tmp/obscura-target-arch`, whereas `build_obscura_arch.sh` builds with `--profile release-dist`.
**Why it matters**: When cargo builds with `--release`, artifacts are emitted to `/target/release/`, whereas `--profile release-dist` emits to `/target/release-dist/`. Following the command documented in `verify.md` results in missing binary errors during subsequent staging steps expecting `release-dist`.
**Suggested fix**: Update line 185 in `verify.md` to specify `--profile release-dist` instead of `--release` to match `build_obscura_arch.sh`.

### 🟡 Unstripped binary size in Arch Linux provenance record, `backend/scripts/build_obscura_arch.sh:47`
**Problem**: `build_obscura_arch.sh` copies and measures the Obscura binary size for `provenance.json` without running `strip --strip-unneeded`, unlike `build_obscura_al2023.sh` which strips symbols first.
**Why it matters**: The binary hash and byte size recorded in Arch Linux `provenance.json` reflect the unstripped build containing debug symbols, creating a noticeable discrepancy with the stripped binary packaged into deployment artifacts.
**Suggested fix**: Add `strip --strip-unneeded` to `build_obscura_arch.sh` before calculating `OBSCURA_HASH` and `OBSCURA_SIZE`.

## Nits
- ⚪ `backend/scripts/build_obscura_al2023.sh:36`, The script exits immediately if the source checkout is dirty or at an unexpected SHA without offering interactive cleanup advice or options to reset the cache.

## Strengths
- Production orchestration acceptance: `test_obscura_production_orchestration_e2e` exercises the full production pipeline through `GraphRunnerService`, checkpointer, outbox dispatch, real Obscura worker rendering under lease fencing, and direct production graph state inspection without artificial test only database queries.
- Strict containment in packaging verification: `verify_browser_startup_and_render_isolated_environment` strictly confines executable paths to the artifact directory and eliminates host PATH fallback, preventing false positives from host binaries.
- Resource profiling and metric transparency: Process tree memory sampling every 20ms provides accurate tracking of peak RSS across host, driver, supervisor, and worker processes while separating startup, navigation, and total task durations, with honest acknowledgment of unverified comparative claims.
- Cryptographic provenance and build integrity: Both build scripts enforce pinned git commit verification and emit structured `provenance.json` records with SHA256 digests.

## Test coverage
Browser test coverage is comprehensive and thorough. All 197 unit, live, and integration tests pass cleanly with zero failures (193 unit and live tests, and 4 integration tests). The test suite thoroughly covers negative packaging verification cases (missing artifact binary, host PATH rejection, external override rejection, malformed outputs), end to end orchestration flow with checkpoint resumption, and process tree memory measurement. Static analysis is clean with zero ruff lint warnings and zero mypy type check issues across all source files.

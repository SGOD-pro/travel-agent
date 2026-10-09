# Verification report: graph runner checkpoint protection under spec 0001

## /check verify graph runner checkpoint protection fixes under 0001 (PASS)

**PASS: all 8 runtime checkpoint protection behaviors met on live PostgreSQL with real LangGraph execution.**

Next: /check review (fresh model read of the checkpoint protection diff) or /develop the remaining message consumer wiring slices. Spec 0001 remains pending because external queue consumers and deployment wiring are intentionally unbuilt in this slice.

### Verified runtime behaviors and evidence

1. Fresh process restart restores checkpoints and pending writes: PASS
   Executed graph start step, then instantiated a completely fresh checkpointer with zero in memory dictionaries. Loaded state via `aget_state()` and `aget_tuple()`. Proved PostgreSQL restores the complete graph state and pending writes without relying on in memory caches.
   Evidence: `verify_checkpoint_protection.py::check_1_fresh_process_restart` and `test_checkpoint_protection.py::test_fresh_checkpointer_reads_persisted_state`.

2. Buffered checkpoints remain visible within the same invocation while persisted state remains authoritative across invocations: PASS
   Wrote uncommitted state into checkpointer buffer. Proved it is immediately visible to local queries in that invocation, while an external checkpointer process sees zero changes in PostgreSQL until flush commits.
   Evidence: `verify_checkpoint_protection.py::check_2_buffered_checkpoints_same_invocation_vs_persisted`.

3. Expired or replaced owners cannot flush checkpoints or pending writes: PASS
   Runner A paused during Phase 2. Runner A lease expired. Runner B claimed execution ownership with a fresh lease token, advanced the graph, and committed. Runner A woke up and attempted Phase 3 commit. Proved Runner A was rejected with zero tasks dispatched and zero checkpoint writes committed over Runner B progress.
   Evidence: `verify_checkpoint_protection.py::check_3_expired_owners_cannot_flush_checkpoints` and `test_checkpoint_protection.py::test_expired_runner_takeover_stale_writes_rejected`.

4. Checkpoint flush, task creation, and outbox insertion roll back together: PASS
   Simulated database error inside Transaction 2 after checkpoint flush. Proved rollback removes all checkpoint rows, tasks, and outbox rows, and discarded the in memory buffer to prevent leakage.
   Evidence: `verify_checkpoint_protection.py::check_4_checkpoint_flush_and_outbox_atomic_rollback`.

5. Runner and reconciliation overlap cannot lose outputs or graph progress: PASS
   Executed graph to waiting tasks, completed worker task with worker fencing, reconciled outcome, and resumed graph to successful completion. Proved parent first row locks serialize access and preserve all outputs.
   Evidence: `verify_checkpoint_protection.py::check_5_runner_reconciliation_overlap_preserves_outputs` and `test_checkpoint_reconciliation.py::test_successful_result_reconciliation_and_marker_recorded`.

6. Discarded buffers cannot leak between concurrent or later invocations: PASS
   Buffered data in an aborted invocation, discarded it, then ran a subsequent valid invocation on the same checkpointer. Proved zero stale residue from the aborted invocation reached PostgreSQL.
   Evidence: `verify_checkpoint_protection.py::check_6_discarded_buffers_do_not_leak` and `test_checkpoint_protection.py::test_rejected_buffer_does_not_leak_into_subsequent_invocation`.

7. Expired execution recovery emits one durable redispatch and is called by the bounded recovery entry point: PASS
   Created execution in RUNNING state with expired lease. Called `run_recovery_loop` with bounded runtime. Proved the bounded loop recovered the execution, reset lease ownership, emitted exactly one EXECUTION_START outbox event, and recorded an EXECUTION_LEASE_RECOVERED job event.
   Evidence: `verify_checkpoint_protection.py::check_7_expired_recovery_redispatch_and_entry_point` and `test_checkpoint_protection.py::test_concurrent_execution_recovery_produces_one_redispatch`.

8. Deadline failure atomically records status, outbox, and timeline events: PASS
   Triggered reconciliation on an execution with an expired deadline. Proved reconciliation sets FAILED status, inserts an EXECUTION_FAILED outbox event, and records an EXECUTION_FAILED job event in a single atomic transaction without duplicates on replay.
   Evidence: `verify_checkpoint_protection.py::check_8_deadline_failure_atomic_events` and `test_checkpoint_protection.py::test_deadline_failure_events_and_rollback_consistency`.

### Test execution summary

- Runtime verification script: 8 of 8 passed in 75.38s (`verify_checkpoint_protection.py`)
- Regression protection suite: 5 passed in 42.23s (`tests/integration/test_checkpoint_protection.py`)
- Graph runner suite: 6 passed in 103.57s (`tests/integration/test_graph_runner.py`)
- Checkpoint reconciliation suite: 11 passed in 95.11s (`tests/integration/test_checkpoint_reconciliation.py`)
- Task recovery suite: 19 passed in 122.51s (`tests/integration/test_task_recovery.py`)
- Static analysis: `uv run ruff check` passed with zero errors across all touched files

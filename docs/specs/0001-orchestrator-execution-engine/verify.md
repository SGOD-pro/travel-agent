# Verify: Orchestrator Execution Engine · spec 0001 · updated 2026-10-08

_Steps derived from spec 0001 acceptance criteria and specced surfaces. `/check verify` runs these; `/test` locks the durable ones._

## Acceptance criteria

- [x] AC-1: Submitting creation request with existing idempotency key and identical input hash returns original response without second insert → verified via `test_job_acceptance_idempotency.py::test_fresh_job_acceptance_atomic_transaction`, `test_concurrent_identical_requests`, `test_api_trip_plans_endpoint_idempotency_integration`
- [x] AC-2: Submitting creation request with conflicting input hash returns HTTP 409 conflict → verified via `test_job_acceptance_idempotency.py::test_conflicting_payloads_raises_409`, `test_concurrent_conflicting_requests`
- [x] AC-3: Concurrent requests arriving with identical idempotency keys serialize deterministically without unique constraint error → verified via `test_job_acceptance_idempotency.py::test_concurrent_identical_requests`
- [x] AC-4: Execution queries, status checks, cancellations, and task operations enforce owner verification, returning HTTP 404 on owner mismatch → verified via `test_job_acceptance_idempotency.py::test_owner_isolation`
- [x] AC-5: Enqueuing tasks writes task records and corresponding outbox_events rows in same database transaction → verified via `test_job_acceptance_idempotency.py::test_rollback_after_outbox_insert`, `test_rollback_after_execution_insert`
- [x] AC-6: Worker claims pending task with fresh lease_token, expiry, and attempt increment on claim only → verified via `test_worker_claim_fencing.py::test_successful_task_claim_and_attempt_increment`
- [x] AC-7: Concurrent worker claims race resolves with exactly one worker acquiring lease while other workers skip row → verified via `test_worker_claim_fencing.py::test_concurrent_claims_race_condition`
- [x] AC-8: Completing task requires matching active lease token, RUNNING status, and unexpired lease; stale worker rejected → verified via `test_worker_claim_fencing.py::test_stale_writes_rejected`, `test_fenced_completion_success`
- [x] AC-9: Requesting execution cancellation transitions status to CANCELLED and sets cancelled_at; late worker results discarded → verified via `test_worker_claim_fencing.py::test_cancelled_parent_rejects_claim_and_completion`, `test_task_recovery.py::test_cancelled_execution_recovery_marks_task_cancelled`, `test_checkpoint_reconciliation.py::test_cancellation_barrier_skips_checkpoint_update`
- [x] AC-10: Worker crash or lease expiry triggers scheduled recovery to reset task to PENDING with retry backoff and unchanged attempt counter → verified via `test_task_recovery.py::test_retry_backoff_eligible_recovery_does_not_increment_attempts`, `test_concurrent_recovery_synchronization`
- [x] AC-11: Tasks reaching maximum attempt limit transition to FAILED, emit failure outbox event, halt automatic reclaims → verified via `test_task_recovery.py::test_exhausted_attempts_transitions_to_failed`
- [x] AC-12: Outbox relay acquires durable relay leases using skip locked, marks delivered_at, bounds runtime → verified via `test_outbox_relay.py::test_successful_relay_claim_publish_and_delivery_confirmation`, `test_bounded_invocation_duration_and_batch_size`
- [x] AC-13: Completion reconciler records incoming completion events into completion_receipts conflict safely, deduplicating deliveries → verified via `test_checkpoint_reconciliation.py::test_successful_result_reconciliation_and_marker_recorded`, `test_receipt_deduplication_already_applied`
- [x] AC-14: Crash before or after LangGraph checkpoint commit recovers cleanly on replay without duplicate side effects, verified by accepted result markers → verified via `test_checkpoint_reconciliation.py::test_crash_before_checkpoint_update_resumes_unapplied_receipt`, `test_crash_after_checkpoint_update_before_applied_at`

## Specced surfaces

- [x] Table `api_idempotency` → built and live in PostgreSQL (Migration 0003)
- [x] Table `executions` → built and live in PostgreSQL (Migration 0003)
- [x] Table `tasks` → built and live in PostgreSQL (Migration 0004)
- [x] Table `worker_results` → built and live in PostgreSQL (Migration 0004)
- [x] Table `outbox_events` → built and live in PostgreSQL (Migration 0005)
- [x] Table `completion_receipts` → built and live in PostgreSQL (Migration 0006)
- [x] Table `job_events` → built and live in PostgreSQL (Migration 0007, verified via `test_job_surfaces.py::test_acceptance_status_url_resolves_successfully`, `test_timeline_ordering_pagination_and_rollback_consistency`)
- [x] Route `POST /api/v1/trips/{trip_id}/plans` → built and live in `backend/src/functions/api/routes/trips.py`
- [x] Route `POST /api/v1/jobs/{job_id}/cancel` → built and live in `backend/src/functions/api/routes/jobs.py` (Transaction 8, verified via `test_job_surfaces.py::test_repeated_cancellation_and_terminal_job_behavior`, `test_owner_isolation_status_timeline_and_cancellation`)
- [x] Route `GET /api/v1/jobs/{job_id}` → built and live in `backend/src/functions/api/routes/jobs.py` (verified via `test_job_surfaces.py::test_acceptance_status_url_resolves_successfully`, `test_owner_isolation_status_timeline_and_cancellation`)

# Database schema specification

Version 1.0 | Logical/relational design, not executed SQL | Change when data model changes.

## Conventions

PostgreSQL is authoritative. Enable PostGIS in the application's Aiven instance after verifying actual account support. No pgvector initially. UUID primary keys; timestamptz timestamps in UTC; local itinerary times carry IANA timezone. Dates are date fields. Money uses numeric(20,6) plus currency char(3); validate allowed currencies and minor units in application code. FX rates use numeric(24,12). Decimal values serialize as strings. Costs are nonnegative; adjustments, if later supported, require explicit signed type.

## Tables

| Table | Essential columns | Constraints/indexes |
| --- | --- | --- |
| users | id, identity_issuer, identity_subject, created_at, deletion_state | unique(issuer, subject) |
| user_preferences | user_id, preference_key, value_json, consent_id, updated_at | unique(user_id, preference_key) |
| consents | id, user_id, purpose, policy_version, granted_at, revoked_at | index(user_id, purpose) |
| trips | id, owner_id, current_version, state, created_at, updated_at | FK owner; index(owner_id, updated_at) |
| trip_versions | trip_id, version, brief_json, schema_version, command_id, created_at | PK(trip_id,version); unique(command_id) |
| trip_deltas | id, trip_id, base_version, proposal_json, evidence_refs, actor_id, created_at | immutable payload; FK base version |
| delta_events | id, delta_id, sequence, status, validation_json, created_at | unique(delta_id,sequence) |
| approvals | id, delta_id, trip_id, base_version, proposal_hash, actor_id, expires_at, decision, decided_at | one terminal decision; authorized actor |
| planning_runs | id, trip_id, base_version, graph_version, status, started_at, finished_at | index(trip_id,status) |
| provider_registry | id, provider, capability, environment, market, config_json, verified_at, status | unique(provider,capability,environment,market) |
| evidence | id, registry_id, query_hash, evidence_class, data_kind, observed_at, expires_at, source_url, permitted_payload, rights_json, parser_version | class enum; query/time index |
| places | id, source, source_place_id, name, point, access_point, accuracy, rights_json | unique(source,source_place_id); GiST permitted points |
| transport_options | id, run_id, evidence_id, mode, departure, arrival, option_json | FK evidence; index(run_id,mode) |
| hotel_options | id, run_id, evidence_id, place_ref, occupancy_json, check_in, check_out, option_json | check_out > check_in |
| itineraries | id, trip_id, trip_version, run_id, status, created_at | versioned plan; trip/version index |
| itinerary_stops | id, itinerary_id, position, place_id, arrival, departure, timezone, status | unique(itinerary_id,position); departure >= arrival |
| itinerary_legs | id, itinerary_id, position, mode, route_json, distance_m, duration_s, evidence_id | nonnegative distance/duration |
| budget_lines | id, itinerary_id, category, amount, currency, basis, quantity, source_ref, estimated, unknown_reason | null amount iff unknown; no coercion to zero |
| cost_assumptions | id, type, vehicle_class, region, value, unit, source, effective_at, expires_at | positive mileage; versioned |
| fx_rates | id, base_currency, quote_currency, rate, source, observed_at | rate >0 |
| external_booking_intents | id, trip_id, trip_version, option_ref, merchant, context_json, state | no booking confirmation field |
| handoff_events | id, intent_id, actor_id, destination_url, checked_at, event_type | authorized destination; immutable events |
| jobs | id, type, run_id, payload_ref, status, available_at, attempts, lease_until, lease_generation, owner, idempotency_key | unique(type,idempotency_key); claim index(status,available_at) |
| outbox_events | id, aggregate_id, event_type, payload, delivered_at, attempts | pending partial index |
| run_events | run_id, sequence, event_type, payload, created_at | PK(run_id,sequence) |
| artifacts | id, trip_id, trip_version, object_key, status, content_type, expires_at | unique object_key; ownership via trip |
| deletion_jobs | id, user_id, state, progress_json, requested_at, completed_at | auditable per-store completion |

LangGraph-managed checkpoint tables occupy a separate schema and are created/migrated by the selected supported integration. Do not invent its internal schema or couple application SQL to it.

## Canonical brief

brief_json is a versioned Pydantic contract: origin and ordered destinations with provider refs, coordinates and confidence; date range; adults, child ages and rooms; budget; modes; accessibility/dietary requirements; fixed/preferred hotels/vendors; hard constraints/preferences. total_days is derived using the documented calendar-day convention. Itinerary records are separate version-bound outputs, not duplicated mutable JSON roots.

## Commit and concurrency

Commit locks/checks current trip version, revalidates approval and evidence, inserts vN+1 and delta acceptance, updates current_version and inserts outbox in one transaction. Repeated command_id returns the prior result. Conflicts return 409, not last-write-wins. Immutable proposals use append-only status events; no rewriting historical deltas. Graph replay reconciles via command_id after partial checkpoint/domain persistence failure.

## Retention and deletion

Proposed release defaults: ephemeral coordination <=24h; raw GPS not persisted by default; SSE progress 7 days; operational logs 30 days with redaction; artifacts 30 days unless explicitly saved; durable trip/checkpoint retention tied to saved trip and deletion policy. Provider contractual retention overrides these defaults downward. These values are design defaults, not a legal compliance assertion.

User deletion tombstones access first, cancels jobs, removes travel data, checkpoint references, Redis keys and S3 artifacts, then records completion without personal payload. Backups expire on documented provider retention; restore procedures reapply deletion tombstones. Identity account deletion is a separate coordinated operation, not an implicit global delete of a shared SWYRA identity.

# SWENA Operations Runbook & Incident Response Guide

**Version:** 1.0  
**Scope:** Production & Staging Operations, Support Inquiries, Recovery Protocols  
**Classification:** Internal Operational Document

---

## 1. System Architecture & Component Inventory

| Component | Technology | Role & Authority | Failure Impact |
| :--- | :--- | :--- | :--- |
| **Frontend Web** | Next.js 16 (App Router / Turbopack) | BFF, static SSR, client UI, session cookie management. | User interface unreachable; backend remains functional. |
| **API Server** | FastAPI (Python 3.12, Uvicorn) | Stateless HTTP API, auth verification, command dispatch. | API requests return 502/503; background jobs continue. |
| **Background Workers** | Python 3.12 (`JobWorker`) | Asynchronous planning, OR-Tools optimization, PDF export. | Async jobs queue up; existing planned trips remain viewable. |
| **Primary Database** | PostgreSQL 16 + PostGIS (Aiven) | **Authoritative State:** Trips, versions, jobs, outbox, audit logs. | Complete platform outage (fail-closed). |
| **Ephemeral Cache** | Redis (Upstash / Compatible) | Session cache, rate limiting, temporary progress events. | Graceful degradation; zero data loss; DB takes load. |
| **Object Storage** | AWS S3 (Private Bucket) | Stored export artifacts (PDFs), spatial GeoJSON snapshots. | Download links fail; plans remain viewable in UI. |
| **Identity Gateway** | SWYRA Auth ([SGOD-pro/OAuth2.1](https://github.com/SGOD-pro/OAuth2.1)) | Sovereign OAuth 2.1 / OIDC identity provider. | New logins fail; existing valid sessions continue until expiry. |

---

## 2. Standard Service Operations & Maintenance

### 2.1 Starting Services Locally
* **Unified Stack:** `./start.sh all` (starts backend on `8000`, frontend on `3000`).
* **Backend Only:** `cd backend && uv run uvicorn travel.runtime.fastapi.app:app --host 0.0.0.0 --port 8000 --reload`
* **Frontend Only:** `cd frontend && npm run dev`
* **Database Migrations:** `cd backend && uv run alembic upgrade head`

### 2.2 Routine Health & Readiness Checks
* **API Liveness:** `GET http://localhost:8000/health` $\to$ Returns `200 OK` `{ "status": "healthy" }`.
* **API Readiness:** `GET http://localhost:8000/ready` $\to$ Checks active database connection pool and Redis ping.
* **Worker Heartbeat:** Workers update `jobs.lease_until` every 30 seconds. Stale leases ($> 120$s) indicate stalled workers.

---

## 3. Incident Response & Failure Recovery Protocols

### 3.1 Incident Severity Levels
* **SEV-1 (Critical):** Core planning or authorization down; database unreachable; data corruption or security breach. Escalation: Immediate (15-minute response).
* **SEV-2 (High):** Asynchronous workers halted; PDF exports failing; live provider adapter errors causing elevated partial plans. Escalation: 1 hour.
* **SEV-3 (Medium):** Non-critical integration down (e.g. weather advisory offline); UI visual defect. Escalation: 1 business day.

### 3.2 Protocol: Asynchronous Worker Crash & Lease Recovery
* **Symptom:** Planning runs stuck in "Compiling..." state; `planning_runs.status` remains `running` indefinitely.
* **Diagnosis:**
  ```sql
  SELECT id, type, status, attempts, lease_until, owner 
  FROM jobs 
  WHERE status = 'CLAIMED' AND lease_until < NOW();
  ```
* **Mitigation:**
  1. Restart worker container pool: `docker compose restart worker` (or ECS task restart).
  2. The recovery scanner automatically identifies expired leases and resets them to `PENDING`:
     ```sql
     UPDATE jobs 
     SET status = 'PENDING', owner = NULL, lease_until = NULL, attempts = attempts + 1
     WHERE status = 'CLAIMED' AND lease_until < NOW() AND attempts < 5;
     ```
  3. Jobs exceeding 5 attempts are automatically marked `FAILED` with error `EXHAUSTED_RETRIES`, prompting the user with an explicit retry button.

### 3.3 Protocol: Database Connection Pool Exhaustion
* **Symptom:** FastAPI logs `TimeoutError: QueuePool limit of size 20 overflow 10 reached`.
* **Diagnosis:**
  ```sql
  SELECT count(*), state FROM pg_stat_activity GROUP BY state;
  ```
* **Mitigation:**
  1. Identify long-running uncommitted transactions:
     ```sql
     SELECT pid, now() - xact_start AS duration, query 
     FROM pg_stat_activity 
     WHERE state = 'active' ORDER BY duration DESC LIMIT 5;
     ```
  2. Terminate rogue blocking queries: `SELECT pg_terminate_backend(pid);`.
  3. Verify SQLAlchemy engine pool settings: `pool_size=20`, `max_overflow=10`, `pool_recycle=300`.

### 3.4 Protocol: Ephemeral Cache (Redis) Outage
* **Symptom:** Redis connection refused; rate limiting logs warnings.
* **Impact Assessment:** **Non-Fatal.** PostgreSQL is the sole authority for all trips, versions, and jobs.
* **Mitigation:**
  1. FastAPI and Workers automatically bypass cache on connection error (`CachePort` fails open to database reads).
  2. Restart Redis container or verify Upstash endpoint credentials.
  3. Once restored, caches warm up lazily on subsequent user queries.

### 3.5 Protocol: Third-Party Provider Failure / Scraping Block
* **Symptom:** External flight, rail, or hotel searches return 403 Forbidden, 429 Too Many Requests, or Cloudflare challenge pages.
* **Strict Policy:** **Zero bypass of anti-bot protections.**
* **Mitigation:**
  1. Provider adapter automatically catches challenge/block and transitions evidence state to `UNAVAILABLE` with reason `PROVIDER_ACCESS_RESTRICTED`.
  2. Plan compilation continues gracefully, leaving the affected section empty and advising traveler: "Live rail fares currently unavailable. Please verify directly on IRCTC portal."
  3. Trip planning **never crashes** due to an external supplier outage.

---

## 4. Support Inquiry & Contact Triage Workflow

### 4.1 Ingestion Pipeline
1. Traveler submits inquiry on `/contact` (Name, Email, Category, Trip ID, Message).
2. Form posts to `POST /api/v1/support/inquiries`.
3. Backend validates input, generates unique ticket UUID (`INQ-xxxxx`), and inserts record into `contact_inquiries` with status `QUEUED`.
4. Transaction publishes event to `outbox_events` for operational dispatch.

### 4.2 Support Triage Runbook
* **Querying Pending Inquiries:**
  ```sql
  SELECT id, email, category, trip_id, created_at 
  FROM contact_inquiries 
  WHERE status = 'QUEUED' 
  ORDER BY created_at ASC;
  ```
* **Operational SLA Targets:**
  * In-trip emergency / roadside inquiries: **$\le 2$ hours**.
  * General planning feedback and bug reports: **$\le 24$ hours**.
* **Resolving Inquiries:**
  Support agent updates ticket record:
  ```sql
  UPDATE contact_inquiries 
  SET status = 'RESOLVED', resolved_at = NOW(), response_notes = $1 
  WHERE id = $2;
  ```

---

## 5. Security Protocols & Emergency Access Management

### 5.1 Compromised Signing Key / JWKS Rotation
* **Trigger:** SWYRA Auth rotates RS256 signing keys or an unauthorized key disclosure is suspected.
* **Execution:**
  1. Deploy new public key to SWYRA Auth JWKS endpoint (`/.well-known/jwks.json`).
  2. Flush cached JWKS in FastAPI runtime:
     * Restart API instances: triggers immediate re-fetch and cache update of public keys.
  3. Revoke all active `swena_session` cookies by rotating the application cookie signing secret.
  4. Users are safely redirected to `/login` to establish authenticated sessions against the new keys.

### 5.2 Malicious Tenant / Account Quarantine
* **Action:** Immediately block a malicious actor without database downtime:
  ```sql
  UPDATE users 
  SET deletion_state = 'SUSPENDED' 
  WHERE id = 'target_user_uuid';
  ```
* FastAPI auth dependency rejects suspended user IDs with `403 Forbidden` on all routes.

---

## 6. Privacy Deletion & Disaster Recovery Protocols

### 6.1 Executing Account Deletion Request (DPDP / GDPR)
* **Trigger:** User submits deletion command on `/dashboard/settings`.
* **Execution Steps:**
  1. Mark account pending: `UPDATE users SET deletion_state = 'PENDING' WHERE id = $1;`.
  2. Invalidate active session tokens in Redis.
  3. Worker task `execute_account_deletion` runs cascade:
     * Deletes all `trips`, `trip_versions`, `itineraries`, `budget_lines` owned by user.
     * Deletes all public share records in `public_shares`.
     * Identifies associated S3 artifact keys and issues batch delete: `s3.delete_objects(Bucket=BUCKET, Delete={'Objects': keys})`.
     * Purges LangGraph checkpoints for user's thread IDs.
     * Sets `users.deletion_state = 'COMPLETED'` and scrubs email/claims.
  4. Writes auditable event to `deletion_jobs` log.

### 6.2 Disaster Recovery & Database Restoration
* **RPO (Recovery Point Objective):** 1 hour (via automated Aiven hourly snapshots + WAL archiving).
* **RTO (Recovery Time Objective):** 30 minutes to spin up replica.
* **Restore Procedure:**
  1. Provision fresh PostgreSQL instance from verified Aiven PITR (Point-in-Time-Recovery) snapshot.
  2. Verify PostGIS extension is active: `SELECT PostGIS_Version();`.
  3. Re-apply deletion tombstones: Audit log cross-checks ensure previously deleted users are not restored to active state.
  4. Point FastAPI `DATABASE_URL` to restored cluster endpoint and verify health checks.

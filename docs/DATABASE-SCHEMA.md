# SWENA Database Schema Specification

**Version:** 2.0  
**Engine:** PostgreSQL 16 with PostGIS Extension  
**Authority:** Single authoritative persistence store for all domain entities.  
**Migrations:** Managed via Alembic (`backend/alembic/versions/`).

---

## 1. Current Implemented Migrations vs. Proposed Future Schema

### 1.1 Implemented Migrations in Source Code
* **`0001_initial_schema.py`:**
  * `trips`: `(id UUID PK, owner_id UUID NOT NULL, current_version INT NOT NULL, state VARCHAR(50) NOT NULL, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)`. Index on `(owner_id, updated_at)`.
  * `trip_versions`: `(id UUID PK, trip_id UUID NOT NULL FK, version INT NOT NULL, brief_json JSONB NOT NULL, schema_version INT NOT NULL, command_id UUID NOT NULL UNIQUE, created_at TIMESTAMPTZ)`. Unique on `(trip_id, version)`.
  * `jobs`: `(id UUID PK, type VARCHAR(50), payload_ref JSONB, status VARCHAR(20), available_at TIMESTAMPTZ, attempts INT, lease_until TIMESTAMPTZ, lease_generation INT, owner VARCHAR(100), idempotency_key VARCHAR(100) UNIQUE, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)`.
  * `outbox_events`: `(id UUID PK, aggregate_id UUID, event_type VARCHAR(100), payload JSONB, created_at TIMESTAMPTZ, delivered_at TIMESTAMPTZ, attempts INT)`.
* **`0002_add_artifacts.py`:**
  * `artifacts`: `(id UUID PK, trip_id UUID NOT NULL FK, trip_version INT NOT NULL, object_key VARCHAR(255) NOT NULL UNIQUE, format VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL, content_type VARCHAR(100) NOT NULL, expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)`.

### 1.2 Proposed Schema Additions (Milestones C1–C5 Migrations)

| Proposed Table | Essential Columns | Key Constraints & Indexes | Target Milestone |
| :--- | :--- | :--- | :--- |
| `users` | `id UUID PK, identity_issuer VARCHAR(255), identity_subject VARCHAR(255), email VARCHAR(255), deletion_state VARCHAR(20), created_at TIMESTAMPTZ` | `UNIQUE(identity_issuer, identity_subject)` | C1 (`0003_add_users`) |
| `public_shares` | `id UUID PK, trip_id UUID FK, version INT NOT NULL, share_token_hash VARCHAR(64) UNIQUE, projection_json JSONB NOT NULL, expires_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ, created_at TIMESTAMPTZ` | Index on `(share_token_hash, revoked_at)` | C5 (`0004_add_public_shares`) |
| `contact_inquiries` | `id UUID PK, name VARCHAR(100), email VARCHAR(255), category VARCHAR(50), trip_id UUID, message TEXT, status VARCHAR(20), created_at TIMESTAMPTZ, resolved_at TIMESTAMPTZ` | Index on `(status, created_at)` | C6 (`0005_add_contact_inquiries`) |
| `itineraries` | `id UUID PK, trip_id UUID FK, version INT NOT NULL, status VARCHAR(50), plan_json JSONB NOT NULL, created_at TIMESTAMPTZ` | `UNIQUE(trip_id, version)` | C2 (`0006_add_itineraries`) |
| `places` | `id UUID PK, source VARCHAR(50), source_place_id VARCHAR(100), name VARCHAR(255), location GEOGRAPHY(Point, 4326), attribution_json JSONB, created_at TIMESTAMPTZ` | `UNIQUE(source, source_place_id)`, GiST index on `location` | C3 (`0007_add_places_postgis`) |
| `deletion_jobs` | `id UUID PK, user_id UUID NOT NULL, state VARCHAR(20), progress_json JSONB, requested_at TIMESTAMPTZ, completed_at TIMESTAMPTZ` | Index on `(user_id, state)` | C5 (`0008_add_deletion_jobs`) |

---

## 2. Entity Relational Model & Foreign Key Rules

```
       ┌──────────────┐
       │    users     │
       └──────┬───────┘
              │ 1
              │
              │ N (owner_id)
              ▼
       ┌──────────────┐       1:N        ┌─────────────────┐
       │    trips     │ ───────────────► │  trip_versions  │
       └──────┬───────┘                  └─────────────────┘
              │
              ├─────────────── 1:N ─────► ┌─────────────────┐
              │                          │   itineraries   │
              │                          └─────────────────┘
              ├─────────────── 1:N ─────► ┌─────────────────┐
              │                          │    artifacts    │ (PDFs in S3)
              │                          └─────────────────┘
              └─────────────── 1:N ─────► ┌─────────────────┐
                                         │  public_shares  │ (Sanitized projections)
                                         └─────────────────┘
```

---

## 3. Migration & Backfill Strategy (Non-Destructive)

1. **Expand / Contract Pattern:**
   * All schema changes are strictly additive (new tables or nullable columns).
   * No destructive dropping of columns or existing trip versions.
   * Rollback compatibility: Every migration includes a tested `downgrade()` script in Alembic.
2. **Backfill Plan for `users` Table:**
   * When `users` table is migrated in `0003_add_users`, an initial system user (`00000000-0000-0000-0000-000000000001`, `traveler@swena.internal`) is seeded to retain ownership links for historical draft trips created in local testing.
   * New authenticated trips bind to real user UUIDs derived from SWYRA Auth `sub` claims.

---

## 4. Retention & Deletion Lifecycle

* **Active Trips & Versions:** Retained indefinitely until explicitly deleted by trip owner.
* **Ephemeral Coordination (Redis):** Cache keys set with $\le 24$h TTL.
* **Export Artifacts (S3):** Pre-signed download URLs expire in 15 minutes. Artifact database records and S3 objects retained for 30 days unless pinned to saved trip.
* **Audit & Contact Logs:** Retained for 90 days for operational resolution, then hard purged.
* **Account Erasure Cascade (DPDP / GDPR):**
  When a user deletion request executes:
  ```sql
  BEGIN;
  DELETE FROM public_shares WHERE trip_id IN (SELECT id FROM trips WHERE owner_id = $1);
  DELETE FROM artifacts WHERE trip_id IN (SELECT id FROM trips WHERE owner_id = $1);
  DELETE FROM trip_versions WHERE trip_id IN (SELECT id FROM trips WHERE owner_id = $1);
  DELETE FROM itineraries WHERE trip_id IN (SELECT id FROM trips WHERE owner_id = $1);
  DELETE FROM trips WHERE owner_id = $1;
  UPDATE users SET email = 'DELETED@swena.internal', deletion_state = 'COMPLETED' WHERE id = $1;
  COMMIT;
  ```

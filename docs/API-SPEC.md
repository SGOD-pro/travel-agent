# SWENA API Specification

**Version:** 2.0  
**Base URL:** `/api/v1` (FastAPI Resource Server) | `/api/auth` (Next.js BFF Identity Gateway)  
**Protocol:** REST over HTTPS + Server-Sent Events (SSE) for asynchronous streaming.

---

## 1. Wire Casing Convention & Migration Strategy

### 1.1 The Casing Discrepancy
* **Original Documentation (v1.0):** Documented strict JSON `camelCase` (`ownerId`, `currentVersion`, `tripId`).
* **Current Implementation Baseline:** FastAPI Pydantic DTOs implemented standard Python `snake_case` (`owner_id`, `current_version`, `trip_id`, `job_id`).

### 1.2 Canonical Resolution Strategy
To ensure frontend and backend interoperability without brittle, one-sided renames:
1. **Pydantic Model Configuration:** All request and response DTOs use Pydantic v2's alias generator:
   ```python
   from pydantic import BaseModel, ConfigDict
   from pydantic.alias_generators import to_camel

   class BaseSchema(BaseModel):
       model_config = ConfigDict(
           alias_generator=to_camel,
           populate_by_name=True,  # Accepts both camelCase and snake_case on input
           serialize_by_alias=True,  # Serializes to camelCase on the wire
       )
   ```
2. **Wire Standard:** Wire JSON defaults to `camelCase` for all new client-server contracts.
3. **TypeScript Alignment:** Frontend types in `frontend/src/types/api.ts` are generated directly from the OpenAPI schema (`GET /openapi.json`).

---

## 2. Standard Headers & Error Response Envelope

### 2.1 Request Headers
* `Authorization: Bearer <oauth_jwt_token>` (Required for all private `/api/v1/*` routes).
* `Idempotency-Key: <uuid>` (Required on state-mutating POST commands to prevent duplicate execution).
* `Content-Type: application/json`

### 2.2 Standard Error Envelope
All error responses return structured JSON matching this schema:
```json
{
  "code": "VERSION_CONFLICT",
  "message": "Trip version 1 has been superseded. Current server version is 2.",
  "details": {
    "expectedBaseVersion": 1,
    "currentServerVersion": 2
  },
  "requestId": "req_01J8Z4G6K9M2N1P",
  "retryable": false
}
```

### 2.3 Status Code Semantics
* `200 OK`: Synchronous read or successful mutation.
* `201 Created`: Aggregate created (e.g. initial trip brief commit).
* `202 Accepted`: Long-running async job acknowledged (e.g. plan compilation, PDF export).
* `400 Bad Request`: Malformed syntax or invalid parameter.
* `401 Unauthorized`: Missing or invalid Bearer token / expired session.
* `404 Not Found`: Resource absent or caller does not hold ownership (prevents resource enumeration).
* `409 Conflict`: Optimistic locking failure (`VERSION_CONFLICT`) or duplicate idempotency payload mismatch.
* `422 Unprocessable Entity`: Semantic validation failure (e.g. invalid date order, unsupported vehicle type).
* `429 Too Many Requests`: Rate limit exceeded; response includes `Retry-After: <seconds>` header.

---

## 3. Complete Endpoint Inventory (Current vs. Target)

| Method & Path | Auth Required | Input Payload | Success Response | Status |
| :--- | :--- | :--- | :--- | :--- |
| **BFF Identity Endpoints** | | | | |
| `GET /api/auth/login` | No | None | `302 Redirect` to SWYRA Auth with PKCE challenge | Implemented |
| `GET /api/auth/callback` | No | `code`, `state` | `302 Redirect` to `/dashboard` with `swena_session` cookie | Implemented |
| `GET /api/auth/me` | Session Cookie | None | `200 OK` `{ user: { id, name, email } }` | Implemented (Hardening signature verification in C1-T01) |
| `POST /api/auth/logout` | Session Cookie | None | `200 OK` (clears cookie) | Implemented |
| **Trip & Planning Endpoints** | | | | |
| `POST /api/v1/trips` | Bearer Token | `CreateTripRequest { brief: TripBrief }` | `201 Created` `TripResponse { id, currentVersion, brief }` | Implemented (Removing client `owner_id` in C1-T02) |
| `GET /api/v1/trips` | Bearer Token | `cursor`, `limit` | `200 OK` `{ trips: TripResponse[], nextCursor }` | Target (C1-T02) |
| `GET /api/v1/trips/{id}` | Bearer Token | None | `200 OK` `TripResponse` | Implemented (Adding ownership auth in C1-T02) |
| `POST /api/v1/trips/{id}/brief` | Bearer Token | `UpdateBriefRequest { expectedBaseVersion, brief, commandId }` | `200 OK` `TripResponse` (or 409 Conflict) | Implemented (Adding ownership auth in C1-T02) |
| `POST /api/v1/trips/{id}/plans` | Bearer Token | `PlanTripRequest { expectedBaseVersion }` | `202 Accepted` `{ runId, statusUrl, eventsUrl }` | Target (Transitioning to true 202 async in C2-T02) |
| `GET /api/v1/runs/{runId}` | Bearer Token | None | `200 OK` `{ status, sectionStates, proposal }` | Target (C2-T02) |
| `GET /api/v1/runs/{runId}/events` | Bearer Token | Header: `Last-Event-ID` | `200 OK` `text/event-stream` (SSE progress stream) | Target (C2-T03) |
| `POST /api/v1/runs/{runId}/cancel` | Bearer Token | `{ reason: string }` | `202 Accepted` `{ status: "cancelling" }` | Target (C2-T02) |
| `POST /api/v1/trips/{id}/approvals`| Bearer Token | `{ deltaId, baseVersion, proposalHash, decision }` | `200 OK` `TripResponse` with version $N+1$ | Target (C4-T02) |
| **Exports & Sharing** | | | | |
| `POST /api/v1/trips/{id}/exports` | Bearer Token | `{ tripVersion, format: "pdf" }` | `202 Accepted` `{ artifactId, status: "pending" }` | Implemented |
| `GET /api/v1/artifacts/{id}` | Bearer Token | None | `200 OK` `{ status: "ready", downloadUrl }` | Implemented |
| `GET /api/v1/artifacts/{id}/download`| Bearer Token | None | `302 Redirect` to pre-signed S3 URL | Implemented |
| `POST /api/v1/trips/{id}/shares` | Bearer Token | `{ version: int }` | `201 Created` `{ shareToken, shareUrl, qrCodeSvg }` | Target (C5-T01) |
| `GET /api/v1/public/trips/{token}` | Public | None | `200 OK` `PublicTripProjection` (sanitized) | Target (C5-T01) |
| `DELETE /api/v1/trips/{id}/shares/{token}`| Bearer Token| None | `200 OK` (revokes link immediately) | Target (C5-T01) |
| **Operational & Evaluation** | | | | |
| `POST /api/v1/support/inquiries` | Public / Rate-limited | `{ name, email, category, tripId, message }` | `201 Created` `{ inquiryId, status: "received" }` | Target (C6-T02) |
| `GET /api/v1/benchmarks` | Public / Ops | None | `200 OK` `{ passedCount, totalCount: 50, results: [...] }`| Implemented |
| `GET /health` | Public | None | `200 OK` `{ "status": "healthy" }` | Implemented |
| `GET /ready` | Public | None | `200 OK` `{ "database": "connected", "redis": "connected" }` | Target (C8-T01) |

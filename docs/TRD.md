# SWENA Technical Requirements Document (TRD)

**Version:** 2.0  
**Status:** Approved Technical Contract  
**Runtime:** Python 3.12 (FastAPI / LangGraph / OR-Tools) + Node.js 22 (Next.js 16 App Router)  
**Database:** PostgreSQL 16 + PostGIS (Aiven) | Ephemeral Cache: Redis (Upstash) | Storage: AWS S3

---

## 1. Runtime Architecture & Module Boundaries

### 1.1 Python Backend Modular Monolith
* **Language Baseline:** Python 3.12.x with strict typing (`mypy --strict`) and Ruff formatting.
* **HTTP Layer:** FastAPI with Pydantic v2 data transfer objects.
* **Workflow Orchestration:** LangGraph state machine owning end-to-end planning sequencing, checkpointing, and human-in-the-loop approvals.
* **Constraint Optimization:** Google OR-Tools (`pywrapcp.RoutingModel`) solving the Traveling Salesperson Problem with Time Windows (TSPTW).
* **Persistence Layer:** SQLAlchemy 2.0 (asyncio extension) with Alembic migrations. Domain models are strictly separated from SQLAlchemy ORM entities via the Repository pattern and Unit of Work (`SqlAlchemyUnitOfWork`).

### 1.2 Mathematical & Monetary Contracts
* **Monetary Representation:** Every monetary value must use Python `Decimal` internally and serialize as a decimal string (e.g., `"1250.00"`) over the API. Floating-point arithmetic for currency is strictly prohibited.
* **Currency Support:** Standard ISO 4217 code (`INR` default). Multi-currency transactions must be converted using a timestamped, verified `fx_rates` record or rejected.
* **Non-Coercion Formula:**
  $$\text{Total} = \sum_{\text{known}} \text{Item} + \sum_{\text{unknown}} \text{Item}$$
  If $\text{Count}(\text{unknown}) > 0$, then $\text{is\_complete} = \text{False}$, and the total is explicitly labeled: `"Known/estimated subtotal; tolls unknown"`. Unknown amounts must NEVER evaluate to $0.00$.

### 1.3 Road Budget Formulation
$$\text{Fuel Liters} = \frac{\text{Route Distance (km)}}{\text{Vehicle Mileage (km/L)}}$$
$$\text{Fuel Cost} = \text{Fuel Liters} \times \text{Fuel Price (₹/L)}$$
$$\text{Road Total} = \text{Fuel Cost} + \text{Highway Tolls} + \text{Parking Fees} + \text{Permits}$$

---

## 2. Bounded Concurrency & Resource Quotas

To prevent resource starvation, memory leaks, and cascading failures, the execution runtime enforces strict per-worker admission limits:

| Resource Pool | Concurrency Limit | Rationale & Enforcement |
| :--- | :--- | :--- |
| **Global Provider I/O** | 8 concurrent tasks / worker | Asyncio `asyncio.Semaphore(8)` capping outbound HTTP sockets. |
| **Per-Origin Web Extraction** | 2 concurrent tasks / domain | Prevents abusive request bursts against third-party partner portals. |
| **Blocking CPU/Disk ThreadPool**| 4 worker threads | Bounded `ThreadPoolExecutor(max_workers=4)` for ReportLab PDF compilation and local disk I/O. |
| **Headless Browser Contexts** | 1 browser context / worker | Playwright/Scrapling chromium process lifecycle strictly bounded to prevent RAM exhaustion. |
| **Database Connection Pool** | 20 pool size + 10 max overflow | SQLAlchemy `QueuePool` sized to stay safely within Aiven PostgreSQL connection limits. |

---

## 3. Verified AWS Lambda Limits & Split Decision Protocol

### 3.1 Official AWS Lambda Limits (Verified 2026-09-12)
* **Deployment Package Size:** 50 MB compressed ZIP direct upload; **250 MB uncompressed deployment contents limit** (including all layers and custom runtime).
* **Container Image Deployment:** Up to **10 GB uncompressed** container image via Amazon ECR.
* **Configurable Memory:** 128 MB to **10,240 MB (10 GB)** in 1 MB increments. CPU allocation scales linearly with allocated memory (dedicated vCPU allocated at 1,769 MB).
* **Maximum Execution Timeout:** **900 seconds (15 minutes)**.
* **Ephemeral `/tmp` Storage:** 512 MB to 10,240 MB.

### 3.2 Runtime Split Protocol (Container vs. Lambda)
Because Google OR-Tools and headless browser binaries exceed the 250 MB uncompressed ZIP limit, **ECS containers are the primary long-running execution runtime**. Workloads may be extracted to Lambda functions only when all of the following criteria are satisfied:
1. **Profiling Evidence:** Measured uncompressed package size $\le 250$ MB (or packaged as an ECR container image).
2. **Cold-Start Latency:** Measured P95 cold-start overhead $\le 1.5$s.
3. **Execution Horizon:** Task executes within a predictable bounded window $\le 60$s (e.g. PDF export artifact generation).
4. **Zero Shared In-Memory State:** Task operates purely as an idempotent consumer of database job IDs.

---

## 4. Asynchronous Job Lifecycle & State Transitions

Planning runs, PDF exports, and account data deletions execute as durable asynchronous jobs managed via PostgreSQL row locks (`SKIP LOCKED`).

```
              ┌─────────────┐
              │   PENDING   │ ◄────────────────────────┐
              └──────┬──────┘                          │
                     │ (Worker claims lease)           │ (Lease expires / Worker crash)
                     ▼                                 │
              ┌─────────────┐                          │
              │   CLAIMED   │ ─────────────────────────┘
              └──────┬──────┘
         ┌───────────┴───────────┐
         │ (Success)             │ (Exception / Retries < 5)
         ▼                       ▼
  ┌─────────────┐         ┌─────────────┐
  │  COMPLETED  │         │   FAILED    │ (Terminal if retries >= 5)
  └─────────────┘         └─────────────┘
```

### 4.1 State Invariants
1. **Atomic Claiming:** Workers claim available jobs using:
   ```sql
   SELECT id, type, payload_ref 
   FROM jobs 
   WHERE status = 'PENDING' AND (available_at IS NULL OR available_at <= NOW())
   ORDER BY available_at ASC 
   FOR UPDATE SKIP LOCKED 
   LIMIT 1;
   ```
2. **Lease Fencing:** When claiming, the worker updates `lease_until = NOW() + INTERVAL '60 seconds'` and increments `lease_generation`. Updates from a worker whose lease has expired are rejected.
3. **Poison Job Quarantine:** Jobs that fail 5 consecutive attempts transition to `status = 'FAILED'` with a recorded error trace. They are never retried automatically, preventing infinite worker crash loops.

---

## 5. Third-Party Provider Search & Extraction Pipeline

```
[Target Destination / Query]
       │
       ▼
[Canonical Host & Port Validation] ──(Check Domain Allowlist)──► [Block if Unapproved]
       │
       ▼
[Async HTTP Fetch / Permitted Headless Render]
       │
       ▼
[Anti-Bot / CAPTCHA Detection] ──(If Detected)──► [Transition to UNAVAILABLE (Zero Bypass)]
       │
       ▼
[Structured Selector Extraction] ──(Normalize Fare, Room, Date, Inclusions)
       │
       ▼
[Context Matching & Verification] ──(Party Size & Date Check)
       │
       ▼
[Classification: LIVE_OFFER | INDICATIVE_SEARCH | EDITORIAL_DISCOVERY]
```

### 5.1 Anti-Bot Policy
Under no circumstances may the application attempt CAPTCHA bypass, IP rotating proxy abuse, or header forging to evade supplier anti-bot defenses. If an external supplier blocks automated queries, the adapter must report `UNAVAILABLE` with status `PROVIDER_ACCESS_RESTRICTED`.

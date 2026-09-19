# SWENA Deployment, Packaging & Operational Architecture

**Version:** 2.0  
**Status:** Approved Infrastructure Baseline  
**Primary Target:** Amazon ECS (Fargate Container Tasks) with Aiven PostgreSQL and Upstash Redis.  
**Secondary / Local Targets:** Docker Compose, Single EC2 Host, Bounded AWS Lambda Functions.

---

## 1. Deployment Profiles

| Deployment Profile | Application Server (FastAPI) | Background Workers (`JobWorker`) | Database Authority | Ephemeral Cache | Use Case |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Local Development** | Local process (`uv run uvicorn`) | Local process or Docker container | Local PostgreSQL 16 + PostGIS | Local Redis (Port 6379) | Day-to-day coding and unit test verification. |
| **Local Docker** | Docker Compose service (`api`) | Docker Compose service (`worker`) | Docker container (`postgis/postgis`) | Docker container (`redis:7`) | Complete offline system verification. |
| **Production ECS (Primary)** | AWS ECS Fargate Task (`swena-api`) | AWS ECS Fargate Task (`swena-worker`)| Aiven PostgreSQL 16 (Multi-AZ) | Upstash Redis (REST / TLS) | Scalable production cloud hosting. |
| **Production EC2 (Alternative)** | Unified Docker Compose on EC2 | Unified Docker Compose on EC2 | Aiven PostgreSQL 16 | Upstash Redis | Budget-conscious single-instance deployment. |
| **Serverless Lambda (Auxiliary)**| Selected thin handlers (`pdf_export`) | Bounded task dispatchers | Aiven PostgreSQL 16 | Upstash Redis | Cold-path, bursty tasks after profiling. |

---

## 2. Verified AWS Lambda Quotas & Split Protocol

### 2.1 Official Verified AWS Lambda Quotas (Verified 2026-09-12)
* **Direct ZIP Upload:** 50 MB compressed archive.
* **Uncompressed Contents Limit:** **250 MB total deployment contents limit** (including function code, all Lambda layers, and custom runtime binaries).
* **Container Image Size:** Up to **10 GB uncompressed** container image via Amazon ECR.
* **Configurable RAM Allocation:** 128 MB to **10,240 MB (10 GB)**. Dedicated vCPU allocated at 1,769 MB.
* **Maximum Execution Duration:** **900 seconds (15 minutes)**.
* **Ephemeral `/tmp` Storage:** 512 MB to 10,240 MB.

### 2.2 Lambda Workload Split Protocol
Because Python dependencies including Google OR-Tools (`ortools`), PostGIS libraries, and headless browser drivers easily exceed the 250 MB uncompressed ZIP limit, **monolithic splitting into dozens of Lambda micro-functions is strictly prohibited**. 

A task may be extracted into a standalone Lambda function only if:
1. **Measured Package Footprint:** Uncompressed artifact size with required dependencies is verified $\le 250$ MB.
2. **Cold-Start P95:** Warm/cold initialization latency is benchmarked and verified $\le 1.5$s.
3. **Execution Horizon:** Task runtime is bounded and predictable ($\le 60$s).
4. **Current Candidate:** ReportLab PDF export artifact generation (`runtime/lambda_handler.py`).

---

## 3. Container Packaging & CI/CD Pipeline

### 3.1 Multi-Stage Dockerfile Architecture
* **Stage 1 (Builder):** Uses `python:3.12-slim`, installs `uv`, creates virtualenv, and pre-compiles wheel dependencies.
* **Stage 2 (Runtime):** Copies virtual environment from builder into minimal Debian slim runtime; runs as unprivileged `appuser` (UID 10001); exposes port 8000; includes `curl` for health probes.

### 3.2 Automated CI/CD Pipeline Stages (GitHub Actions)
```
[1. Static Analysis] ──(Ruff Lint + Mypy Strict Typecheck)
       │
       ▼
[2. Deterministic Unit Tests] ──(Pytest 38 passing unit tests)
       │
       ▼
[3. Frontend Turbopack Build] ──(Next.js 16 build + TypeScript check)
       │
       ▼
[4. Container Image Build & Scan] ──(Docker build + Trivy vulnerability scan)
       │
       ▼
[5. Staging Database Migration] ──(Alembic upgrade head once-only job)
       │
       ▼
[6. Playwright E2E Verification] ──(Headless browser journey test suite)
       │
       ▼
[7. Production Canary Rollout] ──(ECS rolling update with 200% max / 100% min healthy)
```

---

## 4. Graceful Shutdown & Recovery Protocols

1. **FastAPI Graceful Drain:** On receiving `SIGTERM`, Uvicorn halts accepting new incoming TCP connections, allows in-flight HTTP requests 30 seconds to complete, and flushes database connection pools.
2. **Worker Lease Recovery:** Background workers intercept `SIGTERM`, cleanly release active leases in PostgreSQL (`status = 'PENDING', owner = NULL`), and shut down child threads. Stalled jobs whose workers crashed abruptly are automatically reclaimed by the recovery scanner once `lease_until < NOW()`.
3. **Rollback Strategy:** Application container rollbacks are zero-downtime rolling updates. Database schema migrations adhere strictly to the expand/contract pattern, ensuring that rolling back application container versions does not require destructive database rollbacks.

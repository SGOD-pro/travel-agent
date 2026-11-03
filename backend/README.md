# Travel Planning Platform — Backend

Python 3.12 modular backend built with clean architecture, strict domain boundaries, and ports & adapters.

## Structure

```text
src/travel/
  domain/         # Pure business models (Money, Trips, Jobs) with zero framework/DB dependencies
  application/    # Ports (CachePort, TripRepositoryPort, JobRepositoryPort, UnitOfWorkPort)
  infrastructure/ # Adapters for Redis (Upstash HTTP and local asyncio) and Settings
  persistence/    # SQLAlchemy 2.0 ORM models, Repositories, Unit of Work, and Alembic migrations
tests/
  unit/           # Fast in-memory unit tests (100% database-free)
  integration/    # Async database and migration tests
```

## Setup & Commands

```bash
# Setup virtual environment and install dependencies
uv venv
uv pip install -e ".[dev]"

# Run tests
uv run pytest -v

# Run linting & type checks
uv run ruff check .
uv run mypy src

# Run database migrations
uv run alembic upgrade head
```

## Public API and Composition

The public API accepts durable jobs through the transactional outbox. Submit an
`Idempotency-Key` header to each job-acceptance endpoint:

```text
POST /api/v1/trips
POST /api/v1/trips/{trip_id}/plans
POST /api/v1/discovery/media
POST /api/v1/optimization/routes
GET  /api/v1/jobs/{job_id}
POST /api/v1/jobs/{job_id}/cancel
GET  /api/v1/jobs/{job_id}/events
```

Job requests select products explicitly with `products` (`media`, `travel`,
`route`) in that order. Unselected products are not run. Travel and job access
are scoped to the verified token subject; client-supplied trip owners are not
accepted. Bearer tokens must be RS256-signed and pass JWKS, issuer, audience,
subject, issued-at, and expiry validation.

For local development only, `LOCAL_AUTH_BYPASS=true` allows requests without a
token and maps them to one fixed local development user. The backend rejects
this setting unless `ENVIRONMENT=local`. To use it through the frontend proxy,
set the same flag in the frontend development environment. The dashboard warns
when this mode is active. Never enable it on a shared or publicly reachable
backend; leave it unset (or false) for staging and production.

Set `CORS_ALLOWED_ORIGINS` to a JSON array of trusted browser origins. The
included request limiter is process-local; configure an API Gateway/WAF or
shared rate-limit service for distributed production enforcement. The public
API `handler` is an ASGI application entrypoint and requires an ASGI-capable
deployment adapter.

## Media and routing providers

Media discovery accepts public YouTube watch URLs through captions only; it
does not download YouTube video. Videos without accessible public captions
cannot be analyzed, and YouTube may block caption requests from cloud-hosted
backend IPs. Caption text alone is not sufficient to verify a destination, so
travel planning must use manually entered destinations or independent media
evidence. Direct image/audio/video file URLs remain supported separately.
Configure `MEDIA_ALLOWED_HOSTS` as a JSON list of exact trusted hostnames for
those direct downloads. Downloads reject redirects, non-public DNS results,
non-media content types, and files over 50 MiB. The local runtime also needs
FFmpeg for direct video/audio containers; still images do not require it.

For real analysis, set `MEDIA_ANALYSIS_PROVIDER=aws`, configure
`AWS_MEDIA_BUCKET`, and grant the backend's IAM role the required Textract,
Transcribe, and S3 permissions. AWS Transcribe needs to read the uploaded audio
and write its transcript to that bucket. Set a short S3 lifecycle policy for
the `media-analysis/` prefix because media artifacts may contain personal data.
Local `mock` mode never fabricates transcript text or verified places.

Route optimization requires `GOOGLE_MAPS_API_KEY` and the Google Maps Distance
Matrix API enabled for the associated project. Calls are chunked to stay within
the provider's per-request element limit; billing and quota limits are owned by
the configured Google Cloud project.

To resolve observed media clues with public OpenStreetMap Nominatim, set
`MEDIA_GEOCODER_PROVIDER=nominatim` and provide an identifying
`NOMINATIM_USER_AGENT` with a contact address. The adapter limits requests to
one per second and caches results in process memory. That cache and limiter are
not shared across replicas; use a single controlled service or provide a
shared-cache/rate-limit layer before production use of the public Nominatim
service.

Production settings reject the local provider defaults and require explicit
media, geocoder, routing, and hostname configuration. These checks do not prove
that external credentials, IAM permissions, provider quotas, or DNS allowlist
entries are valid; verify those in the target environment.

The composition E2E tests mock product graph boundaries to verify product
selection and ordering. They are contract tests, not proof of live product
flows. AWS cold-start, memory, latency, distributed rate limiting, full
failure-injection, and free-tier measurements remain deployment gates.

## Lambda Packaging

On Windows, Docker Desktop must be running with Linux containers enabled to build
the Linux-compatible evidence collector and browser Lambda artifacts:

```bash
uv run python -m functions.evidence_collector.packaging
uv run python -m functions.browser.packaging
```

The browser packaging command downloads the pinned Sparticuz Chromium layer into
`build/layers` when it is missing, then verifies its SHA256 before extracting or
using it. The generated `build` artifacts are local and are not committed.

The media extractor SAM template is
`src/functions/media_extractor/template.yaml`. It expects the static x86_64
layer at `build/ffmpeg/ffmpeg-layer` and attaches it to the
`MediaExtractorFunction`. Deploy it with:

```bash
sam build --template-file src/functions/media_extractor/template.yaml
sam deploy --guided
```

The layer must contain executable `bin/ffmpeg` and `bin/ffprobe` files.

The same SAM template also deploys `MediaAnalyzerFunction`, a separate
AWS-backed Phase 10 worker. It provisions a private, encrypted S3 bucket for
temporary Transcribe input/output and grants only the analyzer role's
Textract, Transcribe, and `media-analysis/*` S3 permissions. Build and deploy
the complete stack with:

```bash
sam build --use-container --template-file src/functions/media_extractor/template.yaml
sam deploy --guided --template-file .aws-sam/build/template.yaml
```

The analyzer event may include `frame_images_b64` and `audio_bytes_b64`.
Transcribe temporary objects are deleted after completion, failure, or timeout.
The target AWS account must have Textract and Transcribe enabled; an AWS
`SubscriptionRequiredException` is an account-service activation issue, not a
missing application permission.

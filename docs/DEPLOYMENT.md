# Deployment and performance

Version 1.0 | Profiles specified; nothing deployed.

## Profiles

Local Docker runs API, workers, test PostgreSQL/PostGIS and compatible cache fixtures. ECS is the primary long-running production API/workflow runtime. EC2 runs the same image/commands with host operations managed separately. Lambda packages share application code with thin task handlers. Vercel hosts the single Next.js app. Region, account, domains and actual instance sizes remain deployment configuration, not domain constants.

## Lambda limits verified 2026-09-12

AWS documents 50 MB compressed direct ZIP upload, 250 MB uncompressed deployment contents including layers/custom runtime, container images up to 10 GB uncompressed, memory 128–10,240 MB, and maximum execution 900 seconds. Account quotas can be lower. CPU allocation increases with memory. These are hard/operational limits, not recommended target package sizes. [AWS quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)

Browser binaries and OR-Tools can make ZIP packaging unsuitable. Use task-specific container packaging or container workers after measurement; do not split business rules or add functions solely because a library is large. Local execution cannot establish real Lambda cold-start behavior.

## Split decision protocol

For each candidate (export, notification, enrichment), record dependency size, import/init duration, peak memory, cold/warm latency, throughput, per-invocation cost and end-to-end impact. First remove unused imports, isolate optional dependencies, reuse safe clients, limit concurrency and profile memory settings. Split only when measured isolation/scaling/dependency benefits exceed dispatch, operational and duplicated cold-path costs. Capture before/after evidence in DECISIONS. New features stay modules until a runtime boundary is justified.

## Packaging and CI/CD

Lock frontend/backend dependencies and produce reproducible artifacts. One source tree may produce several packages; local/ECS/EC2 share a common backend image, while Lambda images add their runtime adapter and necessary task dependencies. CI detects affected transitive dependencies: shared-domain changes rebuild/test all affected targets. Do not deploy only a changed handler while leaving its shared dependencies stale.

Pipeline: boundary/schema checks -> unit/contract tests -> PostgreSQL/PostGIS/Redis integration -> fixture evaluations -> image/package build and scan -> staging migration -> staging rollout/smoke -> production release gate. Use GitHub Actions with scoped cloud identity, not long-lived keys in workflow files. Public deployment is a separate action, not implied by documentation creation.

## Database and recovery

Migrations run once as a controlled job. Use expand/contract migrations compatible with overlapping versions; rollback application images without destructive automatic database rollback. Cap each worker pool and Lambda reserved concurrency to fit actual Aiven connections. Keep DB/compute geographically close subject to account availability and privacy needs. Verify backups and restoration; never promise backups for an unverified free tier.

Graceful shutdown stops claims, checkpoints progress and releases/lets leases expire safely. Dispatcher failures leave recoverable jobs. Retry exhaustion persists diagnostic reason. Test crash after domain commit but before checkpoint, and crash after dispatch before acknowledgment. Redis outage loses caches, not trips; rate-limited sources may pause if safe distributed admission cannot be maintained.

## Observability and SLO

Client metrics: submit-to-first-useful-map, submit-to-partial-enrichment, submit-to-final-plan, reroute-to-updated-route. Server metrics separate queue, provider, LLM, solver, persistence and dispatch times. Tag cache state, workload size, deployment profile and provider availability. Starting benchmark envelope: 1–7 day trip, <=3 destinations, <=20 shortlisted POIs per day and <=4 travelers; larger requests are measured separately. This is a performance cohort, not a silent product rejection rule.

Approved targets remain P95 3.5s map, 2s cached reroute, 20s full enriched plan; partial around 10s. No measurements exist. Alert on failure ratios, job age, DB connections, unknown-cost rate, stale evidence, parser drift, unexpected spending and cold-start regression. A provider's latency/failure is visible rather than hidden to satisfy SLOs.

## Cost accounting

Count LLM tokens by model/direction, provider calls, map matrix elements, browser seconds, Lambda duration/memory, container utilization, DB/storage, network and retries. Track cost per accepted plan and per abandoned run. Numerical operating budget and model selection remain to be benchmarked, not copied from the original PDF.

# Review, main, 2026-10-26

**Reviewed by**: Gemini 3.8 Flash (author on Gemini 3.8 Flash)
**Scope**: 6 frontend files, uncommitted changes
**Verdict**: Approve

## Summary

Slice 4 of specification 0006 (`0006-route-graph-and-map-e2e`) implements the frontend Leaflet CorridorMap GeoJSON integration, safe text escaped popups, OpenStreetMap and CARTO tile attributions, an asynchronous polling hook with exponential backoff and refresh recovery, and an interactive route optimizer page. The map correctly flips GeoJSON longitude and latitude coordinate arrays to Leaflet latitude and longitude ordering, renders multi color paths for driving, walking, detours, and road restrictions, and displays live metrics badges for route feasibility, distance, duration, and fuel estimates.

All issues raised during the initial review pass have been resolved cleanly: hook options are stabilized with refs to eliminate re-fetching on keystrokes, the retry handler reuses the destructured `startPolling` callback, marker symbols are sanitized before embedding into HTML strings, and network exceptions trigger a clean circuit breaker after five consecutive failures.

## Resolved findings

* 🟠 **Unstable hook options dependency** (`frontend/src/lib/useRouteOptimization.ts:360`): Resolved by capturing `options` in `optionsRef`, extracting primitive `autoRecover`, and omitting unstable object references from `pollJobStatus` and `useEffect` dependency arrays.
* 🟡 **Direct hook invocation inside event handler callback** (`frontend/src/app/trips/[id]/route/page.tsx:246`): Resolved by replacing `useRouteOptimization().startPolling(jobId)` with `startPolling(jobId)` from the enclosing hook call.
* ⚪ **Marker symbol HTML sanitation** (`frontend/src/components/map/CorridorMap.tsx:332`): Resolved by sanitizing `markerSymbol` with `replace(/[^\w\s-]/g, "").slice(0, 8)` before embedding into `L.divIcon` markup.
* ⚪ **Unbounded network polling retries** (`frontend/src/lib/useRouteOptimization.ts:194`): Resolved by tracking consecutive network failures with `consecutiveErrorsRef` and halting polling with a clear error after five consecutive failures.

## Strengths

* **Strict cross site scripting protection**: `createSafeStopPopup`, `createSafeLegPopup`, and `createSafeRestrictionPopup` construct HTML elements using `document.createElement` and assign untrusted properties strictly through `textContent`, providing robust XSS immunity without external dependencies.
* **Standards compliant Leaflet GeoJSON coordinate handling**: GeoJSON RFC 7946 specifies coordinates in longitude latitude order. `CorridorMap.tsx` correctly flips all coordinate pairs to Leaflet latitude longitude order for markers, leg polylines, restriction polygons, and bounding box calculations.
* **Multi modal visual clarity**: The map provides immediate visual distinction across travel modes with vibrant blue driving polylines, emerald green dashed walking paths, amber parking hubs, semi transparent red avoidance areas for road closures, and high contrast red markers for skipped or infeasible stops.
* **Clean refresh recovery architecture**: The polling hook maintains synchronization with browser URL search parameters and session storage, enabling travelers to refresh the page or navigate away without losing active optimization jobs.

## Test coverage

Test signal: `configured` (Playwright for browser end to end testing, TypeScript typecheck via `tsc --noEmit`, and Next.js build verification).
* `cd frontend && npx tsc --noEmit` exited with code 0, confirming type correctness across all newly created route domain interfaces and React component properties.
* `cd frontend && npm run build` exited with code 0, verifying static optimization and server rendering compilation across all routes including `/trips/[id]/route`.
* Full browser end to end validation of the interactive map, safe popups, and mid trip replanning will be implemented in Slice 5 under AC-11.

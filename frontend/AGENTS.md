<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

# Frontend

## Overview

Next.js 16 App Router frontend for SWENA. Provides interactive multi stop itinerary inspection, route optimization polling, Leaflet geospatial visualization, and mid trip dynamic replanning.

## Key files

| File | Owns |
|---|---|
| `src/components/map/CorridorMap.tsx` | Leaflet map layer consuming RFC 7946 GeoJSON FeatureCollections, multi color paths, pins, parking icons, safe popups, and attribution |
| `src/lib/useRouteOptimization.ts` | Route optimization polling hook with exponential backoff, URL query parameter sync, and session recovery |
| `src/app/trips/[id]/route/page.tsx` | Interactive route optimizer page with live map, mid trip replanning form, completed stops checklist, and version lineage selector |
| `src/types/routing.ts` | GeoJSON and route version history domain TypeScript contracts |
| `e2e/route-optimization-e2e.spec.ts` | Playwright browser end to end test suite |

## Commands

```bash
# Run local development server
npm run dev

# Run production build and typecheck
npm run build && npx tsc --noEmit

# Run browser end to end tests
npx playwright test
```

## Conventions

- Safe popups must construct HTML elements using `document.createElement` and assign untrusted properties strictly through `textContent` to prevent Cross Site Scripting.
- GeoJSON RFC 7946 coordinates `[lon, lat]` must be flipped to Leaflet `[lat, lon]` order for markers, polylines, and bounding box calculations.
- Tile layer attribution for OpenStreetMap and CARTO must be preserved.


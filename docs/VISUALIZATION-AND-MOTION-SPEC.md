# SWENA Visualization, Motion, and Sensory Interface Specification

**Version:** 1.0  
**Design System:** SWENA 2.0 Forest Night (`#0D1915` Background, `#15271F` Surface, `#F7F7F2` Text, `#A9B8AD` Secondary, `#B7C9AD` Sage Accent)  
**Motion Philosophy:** Editorial and evocative for marketing; deterministic, instantaneous, and functional for the planning workspace.

---

## 1. Functional Visualization Matrix

Every visual data display in SWENA is an authoritative representation of underlying domain state, accompanied by textual and accessible fallbacks.

| Visualization Component | Data Binding & User Interaction | Required Fallback & Honest State | Accessibility Contract |
| :--- | :--- | :--- | :--- |
| **Marketing Journey Story** | Showcases a labeled, static example route (Bengaluru $\to$ Coorg $\to$ Wayanad) illustrating brief $\to$ schedule $\to$ budget. | Static, high-contrast narrative cards; explicitly labeled "Illustrative Template". Zero fake live prices. | Screen reader accessible semantic markup (`<article>`, `<figure>`, `<figcaption>`). |
| **Interactive Route Map** | Sourced GeoJSON polylines, sequenced stop markers, and recommended detour POIs with bounding box auto-fit. | If tiles or geometry fail, displays textual stop list and distance matrix with banner: "Map canvas offline; timetable fully operational." | Full keyboard navigation; every marker selectable via numbered stop list; aria-label on map controls. |
| **Daily Itinerary Timeline** | Monotonic timestamps (arrival, dwell, departure) with transit buffers, meal intervals, and opening hours. | Non-overlapping card stack. If transit duration is estimated, displays clock icon with "Calculated at average 50 km/h". | Monotonic ordering guarantees logical DOM order; screen readers read chronological flow without table trap. |
| **Budget Composition** | Itemized categorical breakdown derived from `Decimal` sums: fuel, tolls, lodging, meals, transit, permits. | If an item is unquoted, displays amber pill: "Unknown charge; not added to total". Never renders a zero-width slice implying free. | Accessible HTML table with numerical totals; explicit column for certainty status (`Verified`, `Estimated`, `Unknown`). |
| **Budget Uncertainty View** | Low / Base / High scenarios based on vehicle fuel efficiency range and lodging seasonal bounds. | If bounds cannot be justified by provider evidence, renders single base estimate with explicit assumption ID. | Textual summary explains variance drivers: "Fuel cost varies by $\pm 15\%$ depending on mountain driving conditions." |
| **Alternative Comparison** | Side-by-side comparison of flight vs. train vs. road transit on cost, duration, transfers, and carbon footprint. | If a provider fails, the column renders `UNAVAILABLE` with reason: "No direct rail connectivity between selected points." | Data table with column headers; screen reader announcements for selected option. |
| **Replan Difference View** | Visual diff of updated route vs. base version: green highlight for added stops, strikethrough amber for removed. | Text diff list with clear action buttons: "Accept Proposed Changes" / "Keep Original Plan". No color-only diffing. | Screen reader text explicitly announces: "Added: Madikeri stay 2 days. Removed: Nagarhole transit stop." |
| **Evidence & Provenance Modal** | Displays data source, query parameters, checked timestamp, evidence tier (`LIVE_OFFER`, etc.), and merchant URL. | If source lacks URL, displays institutional reference name and physical address; no fabricated web links. | Focus trapped inside modal; `Escape` key closes modal; focus restored to invoking button. |
| **Corridor Weather Advisory** | Regional elevation, seasonal monsoon index, and Western Ghats hazard warnings (fog, landslide alerts). | Displays IMD or Open-Meteo attribution. If offline, displays seasonal guidance table: "Monsoon season: heavy rain typical July–Sept." | High contrast alert icons (`AlertTriangle`, `CloudRain`) with text description; no animated flashing. |
| **Planning Progress Tracker** | Real-time stage progress reflecting server-sent events: Route $\to$ Lodging $\to$ Places $\to$ Budget $\to$ Synthesis. | Step indicator with clear textual state ("Resolving transit corridors..."). Zero simulated percentage bars. | `aria-live="polite"` region announces each completed stage to screen readers. |
| **Public Itinerary & QR Code** | High-entropy public share URL encoded into client-side SVG QR code with download/copy actions. | If QR rendering fails, user can copy plain HTTPS link. Revoked link renders 404 page with clean explanation. | QR code contains `alt="QR code to access trip on mobile devices"` and includes visible text URL below. |

---

## 2. Motion System & Performance Budget

### 2.1 Separation of Concerns: Marketing vs. Workspace
1. **Marketing Site (`/`, `/about`, `/contact`):**
   * Expressive, scroll-driven editorial motion powered by GSAP and Lenis.
   * Atmospheric depth, 3D card tilt, and staggered typography entrances.
   * Strictly non-blocking: motion never delays time-to-interact (TTI).
2. **Planning Workspace (`/dashboard`, `/dashboard/*`):**
   * Calm, instant, functional UI.
   * Lenis smooth scrolling is **disabled** inside the planning workspace to prevent lag in nested map, canvas, and form viewports.
   * Transitions are limited to fast CSS transforms ($\le 200$ms) or immediate state updates.

### 2.2 Animation Specifications Table

| Animated Element | Purpose & Function | Trigger | Duration & Easing | Affected Properties | Desktop vs. Mobile Behavior | Reduced-Motion State | Cleanup & Memory |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **3D Travel Card** (`travel-card-3d.tsx`) | Evoke physical depth and topography on signature corridor cards. | Pointer move across card surface. | 0.35s `power2.out` tracking; 0.6s `power3.out` reset on exit. | `rotationX`, `rotationY`, `transformPerspective`, `z` translations. | Active on desktop pointers; disabled on touch devices to avoid scroll jank. | Card remains static flat; hover applies subtle border color change only. | Scoped via `@gsap/react` `useGSAP`; event listeners removed on unmount. |
| **Marketing Hero Entrance** | Establish high-craft editorial aesthetic on initial visit. | Page mount / initial scroll into viewport. | 1.1s `power3.out` headline; 0.9s subhead. | `y` (40px $\to$ 0px), `opacity` (0 $\to$ 1). | Matches across desktop and mobile. | Instant opacity 1; `y` offset 0; zero delay. | Timeline paused and cleared on unmount. |
| **Corridor Selector Pill** | Indicate active corridor selection in interactive demo console. | User click / keyboard activation. | 0.25s `power1.inOut`. | `backgroundColor`, `color`, `borderColor`. | Identical behavior across all viewports. | Instant color switch without transition duration. | CSS transition classes; no JavaScript ticker. |
| **Timeline Re-solve Transition** | Signal to user that schedule timestamps have been updated by solver. | Successful completion of planning run. | 0.4s `power2.out` stagger (0.05s per card). | `y` (10px $\to$ 0px), `opacity` (0.5 $\to$ 1). | Limited to first 5 visible stops on mobile. | Instant swap without stagger or vertical translation. | Timeline killed immediately if user changes tab. |
| **Modal Dialog Entrance** | Focus user attention on evidence, QR code, or booking handoff. | User clicks detail action button. | 0.2s `cubic-bezier(0.16, 1, 0.3, 1)`. | `scale` (0.96 $\to$ 1.0), `opacity` (0 $\to$ 1). | Desktop centers modal; mobile opens responsive bottom drawer. | Instant display at scale 1, opacity 1. | Radix UI Portal handles DOM lifecycle and focus restoration. |

### 2.3 GSAP & Lenis Architecture Rules
* **Unified Animation Loop:** Lenis does not run independent `requestAnimationFrame` loops. When active on marketing pages, Lenis ticker is synchronized directly with GSAP:
  ```typescript
  lenis.on('scroll', ScrollTrigger.update);
  gsap.ticker.add((time) => {
    lenis.raf(time * 1000);
  });
  gsap.ticker.lagSmoothing(0);
  ```
* **Strict Property Selection:** Only composite properties (`transform`, `opacity`) may be animated. Never animate layout-triggering properties (`top`, `left`, `width`, `height`, `margin`, `padding`).
* **Frame Budget:** 60fps minimum on standard mobile devices; 120fps on high-refresh displays. Long animation tasks must not exceed 16.6ms of main-thread execution.

---

## 3. WebGPU / VGPU Guidelines (Optional Progressive Enhancement)

WebGPU / VGPU is an optional visual enhancement reserved exclusively for the marketing hero background (e.g., subtle fluid terrain meshes or particles representing topography). It is strictly prohibited from rendering product data, itineraries, budgets, or maps.

### 3.1 Implementation Constraints
1. **Single Canvas:** Exactly one `<canvas>` element may exist across the entire application.
2. **Feature Detection:** Must explicitly probe `navigator.gpu` before initializing shaders:
   ```typescript
   if (!navigator.gpu) {
     useStaticHeroBackground();
     return;
   }
   ```
3. **Lazy Loading:** Shaders, pipelines, and mesh buffers must be loaded dynamically in the background after Initial Contentful Paint.
4. **Device Pixel Ratio Cap:** Maximum DPR capped at 1.5 to prevent GPU thermal throttling on high-DPI displays:
   ```typescript
   const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
   ```
5. **Offscreen Pause:** When the hero section scrolls out of the viewport, the render loop must pause immediately via `IntersectionObserver`.
6. **Device Loss Handling:** Must listen for `device.lost` and fall back gracefully to the CSS Forest Night gradient background without crashing the browser tab.
7. **Disposal:** All buffers, textures, and pipeline layouts must be explicitly destroyed on React component unmount.
8. **Release Independence:** The application must be 100% operational, fully functional, and visually complete when WebGPU is unavailable or disabled.

---

## 4. Responsive Layout & Accessibility Standards

### 4.1 Responsive Breakpoint Specifications
* **Mobile Viewport (320px – 767px):**
  * Single-column vertical layout.
  * Planning workspace defaults to timeline view; floating action button toggles full-screen map canvas.
  * Stop detail and evidence inspector open as a swipeable bottom sheet.
  * Minimum touch target size: $44 \times 44$ CSS pixels.
* **Tablet Viewport (768px – 1023px):**
  * Stacked two-panel layout (Timeline top, Interactive Map bottom).
  * Collapsible filter sidebar.
* **Desktop Viewport (1024px – 1920px+):**
  * True side-by-side workspace: Timeline left (45% width, independently scrollable), Map right (55% width, sticky full-height canvas).
  * Fixed floating top-bar with trip version, status badge, and primary "Compile" / "Export" CTAs.

### 4.2 Accessibility & Usability Checkpoints
1. **Color Contrast:** All text must satisfy WCAG 2.1 AA minimum contrast ratios:
   * Main text (`#F7F7F2`) on background (`#0D1915`): Ratio $15.8:1$ (exceeds $4.5:1$ AAA).
   * Secondary text (`#A9B8AD`) on surface (`#15271F`): Ratio $5.2:1$ (exceeds $4.5:1$ AA).
   * Sage button text (`#102D25`) on button fill (`#B7C9AD`): Ratio $9.1:1$ (exceeds $4.5:1$ AAA).
2. **Focus Visibility:** Every interactive control features a visible, high-contrast focus ring: `outline: 2px solid #B7C9AD; outline-offset: 2px;`. Focus is never suppressed (`outline: none` without replacement is strictly forbidden).
3. **200% Zoom Compatibility:** Layout must reflow gracefully at 200% browser zoom without horizontal scrolling, overlapping text, or inaccessible action buttons.
4. **Reduced Motion:** When `@media (prefers-reduced-motion: reduce)` matches, all GSAP timelines and CSS transitions execute with `duration: 0` or are disabled.
5. **Slow Network Resilience:** On 3G or offline connections, images and maps display labeled placeholder blocks; core timetable text, cost breakdowns, and merchant handoff links remain immediately readable from server HTML.

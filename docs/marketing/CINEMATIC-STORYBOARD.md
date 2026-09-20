# SWENA Cinematic Website — Storyboard & Motion Architecture

**Document:** `docs/marketing/CINEMATIC-STORYBOARD.md`  
**Updated:** 2026-09-20  
**Creative Thesis:** *The landscape becomes your journey.*  
**Art Direction:** Travel editorial; generous scale, expansive photography, large expressive serif display type, quiet sans body, warm paper transitions, thin editorial rules, restrained terracotta accents, and zero gimmicks (no floating 3D globes, no spaceships, no infinite marquis, no scroll locking).

---

## 1. Design System & Typography Tokens

### Palette
* **Ink:** `#142820` (Authoritative editorial typography and dark forest canvas)
* **Paper:** `#F3EFE6` (Warm editorial paper background for chapters and pauses)
* **Secondary Paper:** `#E8E1D4` (Card accents, ledgers, and subtle tonal borders)
* **Pale Sage:** `#BCC9AF` (Primary restrained interactive action and subtle outlines)
* **Terracotta Accent:** `#C56C4D` (Editorial micro-highlights, illustrative route lines, and stop markers)
* **Forest Surface:** `#15271F` / Border `#233E32` (Elevated dark surfaces)

### Typography
* **Display Serif:** `Bodoni Moda` (Google Fonts, self-hosted via `next/font/google`, `variable: "--font-serif"`).
  * Desktop Hero H1: `clamp(4rem, 9.8vw, 10rem)`, line-height `0.92–1.0`.
  * Section Display Headings: `clamp(2.5rem, 5vw, 4.5rem)`.
* **Editorial Sans:** `Manrope` (Google Fonts, self-hosted via `next/font/google`, `variable: "--font-sans"`).
  * Body: `17–20px` with comfortable `1.6` line-height and max line length ~60 characters (`max-w-prose` / `max-w-2xl`).
  * Captions & Overlines: `11–13px`, uppercase tracking `0.08–0.12em`.

### Spatial Grid & Geometry
* **Grid:** 12-column desktop grid with `5vw` outer gutters (capped at `80px`). Editorial text capped at `1360px`; scenery bleeds full-width.
* **Gutter:** `20px` mobile, `32px` tablet, `5vw` desktop.
* **Vertical Spacing:** `96–144px` between quiet desktop chapters; `56–80px` on mobile.
* **Corners:** Sharp/crisp photography (`0–8px` subtle radius); rounded radii (`12–16px`) reserved strictly for interactive touch targets and badges.

---

## 2. Six Sequential Editorial Scenes

```
[Scene 1: Landscape-to-Route Hero] 
  │  (ScrollTrigger sticky sequence 0.00 → 1.00: Full scenic photo → Paper margin → Animated SVG route)
  ▼
[Scene 2: Destination Filmstrip]
  │  (Three 75vw image spreads: Western Ghats, Rajasthan, Konkan Coast + slug handoff to /dashboard)
  ▼
[Scene 3: Editorial Pause]
  │  (Warm paper #F3EFE6, narrow portrait Coorg coffee detail, statement: "Leave room for the unexpected")
  ▼
[Scene 4: The Plan, Made Tangible]
  │  (Interactive journal: Pace selector, route stop sync, transparent budget ledger with explicit unknowns)
  ▼
[Scene 5: Practical Reassurance]
  │  (Quiet editorial trust principles & accessible accordion FAQ: zero payment locks, evidence tiers)
  ▼
[Scene 6: Closing Scene & Footer]
  │  (Full-bleed Gokarna sunset coastline, "Where will you go next?", restrained brand footer)
```

---

## 3. Detailed Scene Choreography & Motion Contracts

### Scene 1: Arrival and Landscape-to-Route (`HeroJourney.tsx`)
* **Wrapper:** `220svh` desktop scroll container; inner stage `100svh` sticky container.
* **Progress Intervals:**
  * **0.00–0.18:** Full-bleed scenic Kolukkumalai tea hill landscape; H1 (*"Make room for the journey."*), supporting line, and primary actions visible immediately without waiting for JavaScript. Subtle 0.7s entrance reveal.
  * **0.18–0.45:** Main headline autoAlpha recedes; the landscape photograph shrinks gracefully from full-bleed to a crisp left-side framed window (`55vw` width, `75vh` height) resting on warm paper.
  * **0.45–0.75:** The right 45% reveals warm paper with the illustrative corridor route (*Bengaluru → Mysuru → Coorg → Wayanad*). An SVG path dynamically draws with progressive stroke dashoffset through each named stop node.
  * **0.75–1.00:** Stable two-part spread: Landscape left, route line and editorial statement (*"A little structure. More room to wander."*) right.
* **Accessibility / Mobile / Reduced-Motion:**
  * At `< 900px` or when `prefers-reduced-motion: reduce` is active, sticky pinning is removed. The scene displays as a clean natural-flow vertical stack: full landscape hero followed by the route spread.
  * No scroll hijacking. Native touch, mouse wheel, keyboard, and anchor navigation remain unhindered.

### Scene 2: Destination Filmstrip (`DestinationFilmstrip.tsx`)
* **Anchor ID:** `#explore`
* **Intro:** Eyebrow `INSPIRATION CORRIDORS`, H2 `Find your kind of away.`, and one concise supporting sentence.
* **Desktop Strip:** 3 large spreads (`75vw` width each), showing partial peek of the following destination to indicate horizontal continuity:
  1. **Western Ghats:** *01 / 03* — *"Mist, coffee country, and slower mornings."* Action: `Plan a Ghats journey` (routes to `/dashboard?corridor=western-ghats`).
  2. **Rajasthan:** *02 / 03* — *"Courtyards, old cities, and desert light."* Action: `Plan a Rajasthan journey` (routes to `/dashboard?corridor=rajasthan`).
  3. **Konkan Coast:** *03 / 03* — *"Coastal roads and time by the water."* Action: `Plan a coastal journey` (routes to `/dashboard?corridor=konkan`).
* **Desktop Motion:** Driven by GSAP horizontal scroll translation clamped to `scrollWidth - viewportWidth` across a pinned scroll budget of `180vh`.
* **Mobile Interaction:** Native CSS scroll-snap carousel (`scroll-snap-type: x mandatory`) with visible Prev/Next arrow navigation and pagination dots.
* **Keyboard Focus:** Tabbing smoothly scrolls the viewport to reveal the focused destination card.

### Scene 3: Editorial Pause (`EditorialPause.tsx`)
* **Background:** Warm Paper `#F3EFE6`, text Ink `#142820`. No sticky pin.
* **Composition:** 12-column asymmetric split. Left: narrow portrait photograph (`coorg-coffee-detail.jpg`, 4 cols). Right: large Bodoni display statement: *"Leave room for the unexpected."* (8 cols).
* **Copy (Exact 48 words):**  
  *"Most travel tools force a choice between rigid tour packages and dozens of disconnected browser tabs. SWENA brings your driving corridors, daily stops, and budget assumptions into one clear, editable plan—so you spend less time coordinating and more time taking in the morning light."*
* **Motion:** Gentle CSS / GSAP autoAlpha entrance triggered when scrolled into view.

### Scene 4: The Plan, Made Tangible (`TripExample.tsx`)
* **Anchor ID:** `#approach`
* **Heading:** `See a journey take shape.`
* **Interactive Contract (Single Shared Typed Fixture):**
  * **State 1 (Pace):** Toggling between `Unhurried` (3 days / 2 stops / late starts), `Balanced` (4 days / 3 stops / daylight bound), and `In-Depth` (5 days / 4 stops). Visually alters the day timeline stop distribution.
  * **State 2 (Route):** Selecting Day 1, Day 2, or Day 3 highlights the corresponding leg and stop in the route drawing and synchronized timeline.
  * **State 3 (Budget Ledger):** Displays itemized breakdown: Fuel (modeled), Lodging (estimated), Highway Tolls (verified), and Ghat Corridor Permit (`Unknown fee — not added to total`). Unknown values are never coerced to ₹0.
* **Disclaimers:** Labeled `Interactive example` and `Illustrative estimates; your plan may differ.`

### Scene 5: Practical Reassurance (`PracticalFAQ.tsx`)
* **Tone:** Quiet, honest, thin dividing rules, no loud pill buttons or fake review quotes.
* **Questions Answered:**
  1. *How does SWENA generate an itinerary?* (Constraint solving, TSPTW, daylight hours)
  2. *Does SWENA take payments or book tickets directly?* (Strictly non-custodial, direct IRCTC / supplier handoff)
  3. *Are all displayed prices live and guaranteed?* (Evidence tiers: LIVE_OFFER, INDICATIVE_SEARCH, ESTIMATED_MODEL)
  4. *Can I change stops or driving pace after creating a plan?* (Fully editable, dynamic leg recalculation)
  5. *Which regions of India are currently supported?* (Western Ghats, Rajasthan, Konkan Coast)

### Scene 6: Closing Scene and Footer (`ClosingScene.tsx` & `Footer.tsx`)
* **Visual:** Full-width coastal sunset photograph (`konkan-sunset.jpg`, 75svh).
* **Headline:** `Where will you go next?` in Bodoni Moda serif.
* **Action:** `Plan my trip` leading to `/dashboard`.
* **Footer:** Restrained layout with large SWENA wordmark, honest brand statement (*"We gave you memory"*), active About/Contact navigation links, and supplier handoff reassurance.

---

## 4. Reference Adaptations & License Attribution

| Inspected Reference | Source & License | Key Technique Inspected | SWENA Adaptation & Location |
| :--- | :--- | :--- | :--- |
| `3D Parallax Unfurling Gallery` (piyushxdev) | 21st.dev (MIT / Framer Motion) | Multi-column offset parallax with depth perspective | Adapted into `DestinationFilmstrip.tsx` using GSAP ScrollTrigger, featuring 3 Indian destination spreads with mild depth and accessible keyboard controls. |
| `Scroll-Locked Video Hero` (gughigug) | 21st.dev (MIT / Vanilla Canvas/Video) | Viewport-filling media transition into content | Adapted into `HeroJourney.tsx`. **Crucial deviation:** SWENA strictly rejects scroll locking or wheel trapping. Standard document scrolling drives the timeline. |
| `Scroll Morph Hero` (prashantsom75) | 21st.dev (Reference) | Layout morphing between scattered and centered states | Visual inspiration for transitioning from full-bleed photo to framed card. |
| `adaptive-quality` (VGPU) | VGPU Catalog (`vgpu-examples/v1`) | Advisory tier downgrade (`high`/`low`), frame health monitoring, canvas hysteresis | Integrated into `TerrainPointsCanvas.tsx` as a progressive WebGPU/Canvas enhancement behind the hero HTML image. |

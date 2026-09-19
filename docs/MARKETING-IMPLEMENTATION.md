# SWENA Cinematic Marketing Experience Implementation Record

**Document:** `docs/MARKETING-IMPLEMENTATION.md`  
**Date:** 2026-09-19  
**Status:** In Progress (Home Page Chapter Implementation)  
**Art Direction:** Forest Night Palette with Warm Paper Chapters, Editorial Typography (Manrope), Real Indian Destination Photography, Restrained Sage Actions.

---

## 1. Reference Matrix & Connected Resources

| Resource | Observed Technique | Role in Implementation | Dependency Cost | Accessibility Adaptations | License / Provenance |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `hero-carousel` (21st.dev) | Full-bleed photography and filmstrip composition | Inspiration for destination switcher framing; framer-motion rejected in favor of existing GSAP | Zero additional runtime dependencies (uses existing GSAP 3.12) | Manual controls, keyboard tab stops, no autoplay carousel | MIT / Custom reference |
| `sakura-editorial-poster` (21st.dev) | Editorial layered typography and warm paper accents | Headline hierarchy and warm paper contrast break | CSS Tailwind tokens | High-contrast WCAG AA text pairings | Reference only |
| shadcn/ui Registry | Accessible Accordion, Sheet/Dialog, Button, and Card | Maintained primitives for FAQ accordion, mobile navigation drawer, and cards | `@radix-ui/react-accordion`, `lucide-react` | Screen reader aria-attributes, focus traps, Escape handling | MIT |
| Lenis Scroll Engine | Single scroll loop synchronized with GSAP ticker | Marketing page smooth scrolling; explicitly disabled in dashboard | Existing `lenis` v1.1.20 | Reduced motion bypass | MIT |
| GSAP Core & React | Scoped timeline entrances, 3D perspective tilts, and crossfades | Clean animation lifecycles via `useGSAP` | Existing `gsap` v3.12.7 and `@gsap/react` | `prefers-reduced-motion` zero-duration transitions | Commercial Webflow / Free Standard |

---

## 2. Asset Manifest & Destination Photography

| Asset File | Subject & Destination | Source & Provenance | Dimensions & Format | Crop / Focal Point | Alt Text Description |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `public/images/destinations/western-ghats.jpg` | Misty rolling tea and coffee hills, Munnar / Coorg, Western Ghats | Unsplash (Free to use license) | $1600 \times 1067$ JPEG (344 KB) | Center-top landscape | "Misty green hills and tea plantations of the Western Ghats under soft morning light" |
| `public/images/destinations/rajasthan.jpg` | Historic stone courtyard and arched pavilions, Amber Fort, Jaipur, Rajasthan | Unsplash (Free to use license) | $1600 \times 1067$ JPEG (991 KB) | Center archways | "Sunlit sandstone arches and royal courtyards of Amber Fort in Jaipur, Rajasthan" |
| `public/images/destinations/konkan-coast.jpg` | Pristine shoreline, palm cliffs, and Arabian Sea surf along the Konkan coast | Unsplash (Free to use license) | $1600 \times 1067$ JPEG (405 KB) | Center-horizontal sea horizon | "Arabian Sea waves washing against tropical palm-lined cliffs along the Konkan coastline" |

---

## 3. Design Tokens & Visual Tokens

```css
/* Palette */
--bg-forest: #0D1915;          /* Canvas background */
--surface-forest: #15271F;     /* Elevated panel surface */
--surface-hover: #1B3329;      /* Card hover state */
--paper-warm: #F7F7F2;         /* Editorial warm paper chapter background */
--paper-text: #0D1915;         /* High-contrast forest text on paper */
--accent-sage: #B7C9AD;        /* Restrained primary interactive action */
--accent-hover: #C9DBBE;       /* Button hover state */
--btn-text: #102D25;           /* Button text color */
--accent-warm: #D8B78A;        /* Restrained route marker and editorial detail accent */
--text-muted: #A9B8AD;         /* Secondary descriptive copy on forest */
--border-forest: #233E32;      /* Component outlines */
```

---

## 4. Implementation Checklist (Home Page Chapters)

- [x] **H1. Destination-First Hero:**
  - [x] 85–95svh desktop height with subtle contrast scrim.
  - [x] Left-aligned kicker (`INDIA, AT YOUR PACE`), H1 (`Less planning. More remembering.`), supporting line, and two clear actions (`Plan my trip`, `Explore an example`).
  - [x] 3-item destination switcher ("Western Ghats", "Rajasthan", "Konkan coast") with manual keyboard selection and crossfade.
- [x] **H2. Editorial Bridge on Warm Paper:**
  - [x] Warm paper `#F7F7F2` background with `#0D1915` forest typography.
  - [x] Lead sentence: *"A good trip leaves room for the unexpected."*
  - [x] Offset route drawing and concise paragraph explaining the synthesis of routes, stays, and budget assumptions.
- [x] **H3. "Watch a Trip Take Shape" Interactive Story (`#how-it-works`):**
  - [x] 3-step structured walkthrough: (1) Start with your kind of trip, (2) Find a rhythm that fits, (3) See the details before you decide.
  - [x] Sticky visual preview on desktop / responsive vertical cards on mobile.
  - [x] Typed example dataset labeled *"Example itinerary"* with transparent budget placeholders.
- [x] **H4. Destination Journal (`#journeys`):**
  - [x] Heading: *"Where will your next memory begin?"*
  - [x] Asymmetrical editorial spread: large landscape feature + two offset images.
  - [x] Stories: Western Ghats, Rajasthan, Konkan Coast with `Use this inspiration` action.
- [x] **H5. Useful Planning Explained Through Product:**
  - [x] 3 alternating detail rows: (1) Change the plan, keep the trip, (2) Know what is included and what is still an estimate, (3) Choose where you book.
- [x] **H6. Practical FAQ:**
  - [x] Accessible accordion answering 5 honest questions (no payment capture, evidence tiers, coverage).
- [x] **H7. Final Invitation & Shared Footer:**
  - [x] Full-width atmospheric destination banner with *"Make room for the journey."* and primary CTA.

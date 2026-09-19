# SWENA UI/UX Design Brief & Sensory Design System

**Version:** 2.0  
**Design System:** SWENA 2.0 Forest Night  
**Primary Aesthetic:** Editorial, high-contrast, atmospheric green-black tonal family with restrained sage actions. Contrast, not artificial glow or skeuomorphic metallic effects, supplies visual hierarchy.

---

## 1. Color Palette & Typography Tokens

### 1.1 Forest Night Color Hierarchy
```css
:root {
  /* Surface & Background */
  --bg-forest: #0D1915;         /* Primary canvas background */
  --surface-forest: #15271F;    /* Card and panel surface */
  --surface-hover: #1B3329;     /* Interactive hover state */
  --card-forest: #162B22;       /* Elevated dialog and modal background */
  
  /* Borders & Dividers */
  --border-forest: #233E32;     /* High-contrast component borders */
  --border-subtle: rgba(183, 201, 173, 0.15); /* Delicate glassmorphism outlines */
  
  /* Typography */
  --text-main: #F7F7F2;         /* Primary headings and body copy (High contrast) */
  --text-secondary: #A9B8AD;    /* Captions, metadata, and helper text */
  --text-muted: #6E8274;        /* Disabled states and subtle timestamps */
  
  /* Sage Accents & Action Tokens */
  --accent-sage: #B7C9AD;       /* Primary interactive buttons and active tabs */
  --accent-hover: #C9DBBE;      /* Button hover highlight */
  --btn-text: #102D25;          /* High-contrast deep green text inside sage buttons */
  
  /* Status Indicators */
  --status-verified: #B7C9AD;   /* Ground truth / verified supplier evidence */
  --status-estimated: #E9C46A;  /* Modeled / calculated estimate */
  --status-unknown: #F4A261;    /* Explicit unknown charge (non-coerced) */
  --status-hazard: #E76F51;     /* Road closure / weather warning */
}
```

### 1.2 Typography (Google Font Manrope)
* **Heading Typography:** Manrope 600 (SemiBold) with tight tracking (`tracking-tight`).
* **Body Typography:** Manrope 400 (Regular) with open line height (`leading-relaxed`).
* **Emphasis / Badges:** Manrope 500 (Medium) with uppercase letter-spacing (`tracking-wider`).
* **Numerical Data & Codes:** JetBrains Mono or tabular figures (`font-mono`, `tabular-nums`) for currency amounts, timestamps, and coordinates.

---

## 2. Information Architecture & Navigation

```
[ Marketing Landing Page (/) ] ────► [ About Us (/about) ] / [ Contact (/contact) ]
          │
          ▼ (Authentication Gate via SWYRA Auth)
[ Trip Planning Workspace (/dashboard) ]
   ├── Top Bar: Active Trip Name, Version Pill (v1, v2), Status Badge, Compile CTA
   ├── Brief Editor Panel: Origin, Stops, Dates, Party, Budget Ceiling, Mode Selectors
   └── Multi-Tab Solution Workspace:
         ├── Tab 1: Solved Schedule (Chronological Timeline + Weather Advisory)
         ├── Tab 2: Corridor Map (Leaflet Dark Matter Canvas + Polyline Routes)
         ├── Tab 3: Itemized Budget (Non-Coerced Decimal Table + Fuel Calculator)
         ├── Tab 4: Corridor Places (Sourced Viewpoints, Dining, Heritage Stops)
         └── Tab 5: Evidence Registry (4-Tier Evidence Verification Table)
```

---

## 3. Screen & State Specifications

### 3.1 Marketing Home Page (`/`)
* **Hero Section:** High-contrast editorial headline: *"We gave you memory. True Indian road intelligence without synthetic hallucinations."*
* **Signature 3D Corridor Showcase:** Interactive GSAP 3D perspective cards showcasing real Indian corridors:
  1. *Western Ghats Monsoon Odyssey:* Bengaluru $\to$ Mysuru $\to$ Coorg $\to$ Wayanad (340 km, Elevation 900m–2,240m).
  2. *Royal Rajputana Circuit:* Delhi $\to$ Agra $\to$ Jaipur $\to$ Jodhpur (610 km, Heritage Expressways).
  3. *Konkan & Sahyadri Pass:* Mumbai $\to$ Pune $\to$ Mahabaleshwar $\to$ Goa (580 km, Coastal Ghat Curves).
* **Deterministic Console Demo:** Real-time road math calculator demonstrating non-coercion of unquoted toll charges.

### 3.2 Planning Workspace (`/dashboard`)
* **State 1: Initial Draft:** Brief editor populated with sensible defaults or selected marketing corridor; timetable displays empty placeholder with guidance.
* **State 2: Compiling Run:** Asynchronous progress bar reflects Server-Sent Events; stage indicators highlight active worker step (`Routing` $\to$ `Lodging` $\to$ `Budget` $\to$ `Synthesis`).
* **State 3: Solved Proposal:** Timetable displays sequential stops with monotonic timestamps; map canvas renders route polyline; budget table displays categorized breakdown with `"Known/estimated subtotal; tolls unknown"`.
* **State 4: Differential Review:** Edits prompt a differential modal highlighting added stops in sage green and removed stops in amber strikethrough.

### 3.3 Public Shareable Route (`/trips/[id]`)
* **Minimalist Roadside View:** Mobile-first layout designed for smartphone viewing in transit.
* **Sanitized Projection:** Displays timetable, map, and weather advisories; strips owner identity, private notes, and cost breakdowns.
* **Client-Side QR Code:** Instant modal with downloadable SVG QR code for passenger and driver scanning.

---

## 4. Copy Standards & Honesty Rules

| Prohibited Deceptive Copy | Mandatory Honest Copy Standard | Rationale |
| :--- | :--- | :--- |
| `"All costs included: ₹12,400"` (when tolls are unverified) | `"Known/estimated subtotal: ₹12,400; tolls unknown"` | Preserves zero-coercion guarantee; never claims false completeness. |
| `"AI confirmed flight schedule"` | `"Sourced schedule: Flight 6E-204 (Checked 10:15 AM)"` | LLMs cannot confirm schedules; only provider timestamps provide evidence. |
| `"Book Now with Instant Confirmation"` | `"View on IRCTC" / "Search on KSTDC"` | SWENA is not a checkout intermediary; transfers traveler directly to supplier. |
| `"Optimal route guaranteed"` | `"Calculated via OR-Tools constraint solver (Depot return included)"` | Feasible solver solutions must not be exaggerated as globally optimal. |
| `"Audio never leaves device"` | `"Speech processed by browser engine; audio is not stored on SWENA servers"` | Truthful disclosure regarding Chromium Web Speech remote processing. |

---

## 5. Accessibility & Responsive Matrix

* **Touch Targets:** Minimum $44 \times 44$px for all interactive buttons, pills, and map markers.
* **Keyboard Focus:** High-contrast sage outline (`2px solid #B7C9AD`, offset `2px`) visible across all interactive controls.
* **Color Blindness:** State changes never rely on color alone; always paired with explicit text labels and Lucide icons (`CheckCircle2`, `AlertTriangle`, `Clock`, `ShieldCheck`).
* **Screen Reader Flow:** Timetable uses semantic `<ol>` and `<li>` elements ensuring chronological screen-reader navigation.

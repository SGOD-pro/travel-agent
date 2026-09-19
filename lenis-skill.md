---
name: lenis-react-nextjs
description: >
  Production-safe Lenis smooth-scroll integration for React and Next.js
  applications. Use when implementing, debugging, reviewing, or extending
  Lenis scrolling, scroll-linked animation, anchor scrolling, nested scrolling,
  route navigation, GSAP/Framer Motion synchronization, or mobile behavior.
---

# Lenis for React / Next.js

## Mission

Integrate Lenis correctly into React and Next.js applications without
hallucinating APIs, copying obsolete examples, or breaking native scrolling,
accessibility, routing, nested scroll areas, animation systems, or mobile UX.

Lenis is a smooth-scroll layer built on native scrolling. It is useful for
smooth wheel scrolling, scroll-linked animation, parallax, WebGL/3D
synchronization, and animation-system synchronization.

Do NOT treat Lenis as a mandatory dependency for every project. First decide
whether the project benefits from it.

---

# 1. Source-of-truth policy

When implementing Lenis, use this authority order:

1. The installed `lenis` package version in the project.
2. The official Lenis repository and current source:
   https://github.com/darkroomengineering/lenis
3. The official React adapter:
   https://github.com/darkroomengineering/lenis/blob/main/packages/react/README.md
4. The official Lenis README:
   https://github.com/darkroomengineering/lenis/blob/main/README.md
5. Official Lenis release notes:
   https://github.com/darkroomengineering/lenis/releases
6. Official Lenis issues/discussions for unresolved or version-specific
   problems:
   https://github.com/darkroomengineering/lenis/issues
   https://github.com/darkroomengineering/lenis/discussions
7. Next.js official documentation:
   https://nextjs.org/docs
8. GSAP official documentation when GSAP is involved.
9. MDN for browser APIs/CSS behavior.

Do not treat random blog posts, YouTube tutorials, old CodePens, or old GitHub
answers as authoritative.

If an example conflicts with the installed version, prefer the installed
version and verify the actual API in its type definitions/source.

---

# 2. Never hallucinate Lenis APIs

Before using an option or method, verify that it exists in the installed
version.

Current Lenis APIs include names such as:

- `autoRaf`
- `autoResize`
- `autoToggle`
- `allowNestedScroll`
- `anchors`
- `content`
- `duration`
- `easing`
- `eventsTarget`
- `gestureOrientation`
- `infinite`
- `lerp`
- `naiveDimensions`
- `orientation`
- `overscroll`
- `prevent`
- `respectReducedMotion`
- `smoothWheel`
- `stopInertiaOnNavigate`
- `syncTouch`
- `syncTouchLerp`
- `touchInertiaExponent`
- `touchMultiplier`
- `virtualScroll`
- `wheelMultiplier`

Common old/obsolete examples may use names such as:

- `smooth`
- `smoothTouch`
- `gestureDirection`
- older package names such as `@studio-freight/lenis`
- older React wrapper package `@studio-freight/react-lenis`

Do NOT copy those old APIs into a new implementation unless they are actually
present in the project's installed version.

The current package exposes `lenis/react` and the current installation is
`npm i lenis`.

---

# 3. Inspect the application before coding

Before adding or changing Lenis, inspect:

## Framework

- Next.js App Router
- Next.js Pages Router
- plain React/Vite
- React Router inside a React application

## Rendering

- Server Components
- Client Components
- SSR/SSG/CSR
- dynamic imports
- browser-only libraries

## Existing animation systems

Search for:

- `gsap`
- `ScrollTrigger`
- `framer-motion`
- `motion`
- `three`
- `@react-three/fiber`
- existing RAF loops
- existing smooth-scroll libraries

## Existing scroll systems

Search for:

- `Locomotive`
- `SmoothScrollbar`
- `smooth-scroll`
- `scroll-behavior`
- `wheel`
- `touchmove`
- `requestAnimationFrame`
- custom scroll providers

## Layout

Inspect:

- `html`
- `body`
- main scroll wrapper
- `overflow`
- `position: fixed`
- `position: sticky`
- nested `overflow: auto/scroll`
- modals/drawers
- iframes
- carousels
- command palettes
- tables
- editors
- chat panels

Do not install Lenis until the current scroll architecture is understood.

---

# 4. Decide whether Lenis should be used

Use Lenis when there is a real UX/animation requirement such as:

- polished wheel scrolling on desktop
- parallax driven by scroll
- scroll-linked motion
- WebGL/Three.js scenes synchronized to scroll
- GSAP ScrollTrigger synchronized to scroll
- a design explicitly requiring smooth inertial wheel behavior

Prefer native browser scrolling when:

- the product is primarily application-like and contains many independent
  scrollable areas
- smooth scrolling provides little user value
- heavy nested scrolling is the dominant interaction
- accessibility and simple native behavior are more important than visual
  smoothing
- the page is iframe-heavy and iframe interaction is important
- CSS scroll snap is the core interaction and adding Lenis/Snap is unnecessary
- the project already has another global smooth-scroll system
- performance testing shows Lenis is making the experience worse

Do not add Lenis merely because the page "looks modern" or because another site
uses it.

---

# 5. Package installation

Use the project's existing package manager.

npm:

```bash
npm install lenis
```

pnpm:

```bash
pnpm add lenis
```

yarn:

```bash
yarn add lenis
```

Do not change the package manager.

Do not install legacy packages such as:

```text
@studio-freight/lenis
@studio-freight/react-lenis
```

unless the project explicitly requires legacy compatibility and the installed
dependency is intentionally pinned.

When installing, check the version actually resolved by the project.

Do not blindly hardcode a version from an old tutorial.

---

# 6. Recommended CSS

Import the official Lenis stylesheet:

```tsx
import 'lenis/dist/lenis.css'
```

Use the current package stylesheet rather than recreating it from memory.

The current stylesheet contains rules for:

- `html.lenis`
- stopped state
- nested-scroll prevention attributes
- iframe interaction during smooth scrolling
- `autoToggle`

Do not replace the official CSS with arbitrary `overflow: hidden` hacks.

If custom CSS is required, preserve the semantics of the official stylesheet.

---

# 7. Next.js client/server boundary

Lenis is browser-dependent.

Do NOT instantiate Lenis in a Server Component.

Do NOT instantiate Lenis at module scope when that module can execute on the
server.

Create a small Client Component for Lenis infrastructure.

Typical pattern:

```tsx
'use client'

import { ReactLenis } from 'lenis/react'

export function SmoothScroll() {
  return <ReactLenis root />
}
```

Then place this infrastructure at a stable application/root boundary, for
example from a root layout:

```tsx
import { SmoothScroll } from '@/components/SmoothScroll'

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en">
      <body>
        <SmoothScroll />
        {children}
      </body>
    </html>
  )
}
```

Keep the page itself server-rendered when possible.

Do NOT convert the entire application to Client Components just to use Lenis.

A small Client Component is preferable.

---

# 8. Prefer `lenis/react` in React

For React applications, prefer:

```tsx
import { ReactLenis, useLenis } from 'lenis/react'
```

The React adapter creates the Lenis instance and exposes it through context.

For normal root-page scrolling:

```tsx
<ReactLenis root />
```

`root` means Lenis uses the default HTML scroll container and makes the
instance globally accessible through `useLenis`.

Do NOT create a new Lenis instance in every section/component.

Architecture should normally look like:

```text
Root layout
  └── SmoothScroll / ReactLenis
       ├── Header
       ├── Hero
       ├── Sections
       ├── Modal
       └── Footer
```

not:

```text
Hero -> new Lenis()
Gallery -> new Lenis()
Footer -> new Lenis()
```

---

# 9. Basic configuration

Start with the smallest configuration that solves the requirement.

Preferred default:

```tsx
<ReactLenis root options={{ autoRaf: true }} />
```

or simply:

```tsx
<ReactLenis root />
```

Do not blindly add:

```text
duration
easing
lerp
syncTouch
allowNestedScroll
naiveDimensions
infinite
wheelMultiplier
touchMultiplier
```

unless there is a reason.

Lenis already has defaults.

Avoid "magic tuning" from copied portfolio tutorials.

---

# 10. RAF / animation loop rules

There must be exactly one effective animation clock for the Lenis instance.

## Simple application

Use:

```tsx
<ReactLenis root />
```

with the default/automatic RAF behavior.

## Existing custom animation loop

If the application already has a central RAF loop, you may disable Lenis
automatic RAF and call:

```ts
lenis.raf(time)
```

from that clock.

## GSAP

Use GSAP's ticker when GSAP is the animation clock.

```tsx
const lenisRef = useRef<LenisRef>(null)

useEffect(() => {
  const update = (time: number) => {
    lenisRef.current?.lenis?.raf(time * 1000)
  }

  gsap.ticker.add(update)

  return () => {
    gsap.ticker.remove(update)
  }
}, [])
```

Use:

```tsx
options={{ autoRaf: false }}
```

when GSAP owns the ticker.

Do NOT simultaneously run:

```text
Lenis autoRaf
+
requestAnimationFrame
+
GSAP ticker
```

for the same instance.

That creates competing clocks and can create incorrect timing/performance
problems.

---

# 11. GSAP / ScrollTrigger

When ScrollTrigger is present, follow the official Lenis integration.

Core relationship:

```ts
lenis.on('scroll', ScrollTrigger.update)
```

and drive Lenis from GSAP's ticker:

```ts
gsap.ticker.add((time) => {
  lenis.raf(time * 1000)
})
```

The `* 1000` matters because GSAP ticker time is in seconds while Lenis'
`raf()` expects milliseconds.

Do not independently run Lenis through `requestAnimationFrame` when GSAP is
already driving it.

If ScrollTrigger behaves incorrectly:

1. verify the current Lenis integration
2. verify the correct scroller
3. verify the clock
4. verify the official Lenis CSS
5. temporarily remove ScrollTrigger
6. test Lenis alone
7. test without Lenis
8. identify which layer actually causes the problem

Do not blindly add `scrollerProxy()` to every project.

Only use a custom scroller/proxy architecture when the application's actual
scroll container requires it.

---

# 12. Framer Motion / Motion

When using Framer Motion/Motion, avoid multiple systems owning the same frame
clock.

The current React adapter documents a frame-based integration:

```tsx
import { ReactLenis } from 'lenis/react'
import type { LenisRef } from 'lenis/react'
import { cancelFrame, frame } from 'framer-motion'
import { useEffect, useRef } from 'react'

const lenisRef = useRef<LenisRef>(null)

useEffect(() => {
  const update = (data: { timestamp: number }) => {
    lenisRef.current?.lenis?.raf(data.timestamp)
  }

  frame.update(update, true)

  return () => cancelFrame(update)
}, [])
```

Use this only when Motion should own the frame loop.

Do not combine this with Lenis `autoRaf`.

Do not allow Lenis, GSAP, and Motion to all drive the same Lenis instance.

Choose one frame clock.

---

# 13. Scroll event performance

`useLenis()` callbacks can run for every scroll event.

Do NOT put high-frequency scroll data into React state unless it genuinely
needs to cause a React render.

Avoid:

```tsx
useLenis((lenis) => {
  setScrollY(lenis.scroll)
})
```

for animation-heavy interfaces.

Prefer:

- CSS variables
- refs
- GSAP
- direct DOM updates
- Motion values
- WebGL uniforms
- IntersectionObserver for visibility-triggered logic

Use React state for semantic UI state, not as a per-frame animation transport.

Do not perform network requests, large calculations, layout-heavy DOM queries,
or expensive React tree updates inside a scroll callback.

---

# 14. Desktop / large screens

Do not create a special "Lenis for large screens" implementation.

For desktop mouse/trackpad scrolling, the normal behavior should generally be
enough:

```tsx
<ReactLenis root />
```

The important properties are primarily:

```text
smoothWheel
lerp
duration
easing
wheelMultiplier
gestureOrientation
```

Do not assume a larger screen requires a higher `lerp`, longer `duration`, or a
different wheel multiplier.

Tune visually only after testing.

Avoid exaggerated inertia that makes content feel delayed or makes precise
interaction harder.

---

# 15. Mobile / touch behavior

This is critical.

Do NOT assume Lenis should automatically be configured differently for mobile.

Current Lenis behavior smooths wheel input by default. Touch synchronization is
separately controlled by:

```ts
syncTouch
```

Therefore:

```ts
syncTouch: true
```

must NOT be added just because the application has a mobile breakpoint.

Only enable `syncTouch` when the product specifically benefits from Lenis'
touch synchronization/inertia behavior.

Reasons to keep the default on mobile:

- native browser touch scrolling is often preferable
- touch devices already provide their own scrolling behavior
- unnecessary touch interception can make scrolling feel wrong
- mobile performance budgets are tighter
- `syncTouch` has specific compatibility considerations

Test before enabling it.

Test at minimum:

```text
iOS Safari
Android Chrome
slow Android hardware
short page
long page
touch inside nested scroll
touch over modal
touch over carousel
orientation change
```

Do NOT use random recipes such as:

```ts
touchMultiplier: 0
```

to "disable Lenis on mobile".

That is not a general-purpose mobile disable mechanism and can interfere with
input behavior.

If mobile Lenis causes problems, first determine whether Lenis is actually
processing touch input, whether `syncTouch` is enabled, and whether another
system is causing the issue.

---

# 16. When to disable Lenis on mobile

Only disable Lenis on mobile when there is a measured reason, such as:

- mobile smooth behavior is visibly worse than native scrolling
- frame drops are caused by the scroll/animation stack
- a mobile-specific interaction conflicts with the Lenis setup
- the design intentionally requires native touch scrolling

Do not use arbitrary viewport breakpoints such as `768px` without a reason.

If disabling at runtime is required:

1. use a proper `matchMedia` strategy
2. handle resize/orientation changes
3. destroy/recreate or start/stop cleanly
4. do not leak event listeners
5. preserve the same accessibility and routing behavior

Prefer CSS/presentation changes when possible instead of destroying and
recreating the global scroll architecture on every resize.

Do not add complex breakpoint lifecycle logic unless mobile testing proves it is
necessary.

---

# 17. Nested scrolling

Nested scrolling is one of the biggest sources of Lenis bugs.

Examples:

- modal body
- drawer
- sidebar
- table
- chat panel
- code editor
- command palette
- dropdown with scrolling content

By default:

```ts
allowNestedScroll: false
```

Do not blindly set:

```ts
allowNestedScroll: true
```

everywhere.

Lenis documents that `allowNestedScroll` can have performance implications
because nested scrollability is checked during scroll processing.

Prefer explicit prevention for known nested areas.

Example:

```tsx
<div data-lenis-prevent>
  ...
</div>
```

More targeted variants exist:

```text
data-lenis-prevent
data-lenis-prevent-wheel
data-lenis-prevent-touch
data-lenis-prevent-vertical
data-lenis-prevent-horizontal
```

Choose the narrowest attribute that solves the problem.

Use `prevent` for dynamic rules:

```ts
const lenis = new Lenis({
  prevent: (node) => node.closest('[data-scroll-lock]') !== null,
})
```

Do not create a child Lenis instance for every nested element unless the
nested area genuinely needs its own smooth scrolling behavior.

---

# 18. Modal / drawer scrolling

When a modal or drawer contains scrollable content:

- page scrolling should not steal its wheel/touch input
- modal content must remain independently scrollable
- focus behavior must remain accessible
- body/page locking must remain coherent

Prefer:

```tsx
<div data-lenis-prevent>
  <ModalContent />
</div>
```

rather than globally breaking page scroll.

When the modal opens, decide whether the global Lenis instance should be
stopped or simply prevented from consuming modal events.

Use:

```ts
lenis.stop()
```

and:

```ts
lenis.start()
```

only when the application truly wants to stop the global scrolling context.

Do not leave Lenis stopped after the modal closes.

---

# 19. Anchor links

Lenis prevents normal anchor handling unless anchors are enabled.

When an application uses same-page anchors:

```tsx
<ReactLenis
  root
  options={{
    anchors: true,
  }}
/>
```

For a fixed/sticky header, use an appropriate offset or CSS
`scroll-padding-top`/`scroll-margin-top`.

Do not guess the header height.

Calculate or reuse the project's actual design token/variable.

Lenis `scrollTo()` understands CSS scroll margin/padding behavior in current
versions, so do not recreate those calculations manually unless necessary.

For example:

```ts
lenis.scrollTo('#features', {
  offset: 80,
})
```

Only use a custom offset when required.

---

# 20. Next.js navigation

Do not assume Lenis and Next.js route navigation are automatically identical
to native full-page navigation.

Next.js App Router uses client-side transitions and has its own scroll handling.

`<Link>` defaults to:

```tsx
scroll={true}
```

and supports hash navigation.

When Lenis inertia is active during navigation, route-scroll behavior can become
incorrect if the old scroll state carries into the new route.

Before modifying every page, inspect the routing architecture.

Prefer keeping the Lenis provider in the root layout so the instance survives
route changes.

For route transitions that need inertia cancellation, the current Lenis API
supports:

```ts
stopInertiaOnNavigate: true
```

Use it when appropriate rather than inventing a global click interception
system.

Do not blindly call:

```ts
lenis.scrollTo(0, { immediate: true })
```

before every navigation.

That can create visible jumps because the old page is moved before the new
page is ready.

Coordinate route scrolling with the application's actual navigation lifecycle.

When troubleshooting:

1. reproduce with an active Lenis velocity
2. reproduce after Lenis has stopped
3. compare native Next.js navigation
4. inspect `scroll` behavior on `<Link>`
5. verify `stopInertiaOnNavigate`
6. verify hash/anchor navigation
7. verify the scroll container
8. wait for the new page content when custom route scrolling is required

Do not make every route a Client Component solely to reset scroll.

---

# 21. Programmatic scrolling

Use:

```ts
lenis.scrollTo(target, options)
```

instead of creating a second smooth-scroll utility.

Valid targets include:

- number
- selector
- keyword
- HTMLElement

Useful options include:

```text
offset
immediate
lock
lerp
duration
easing
force
onStart
onComplete
userData
```

Use `immediate: true` when a hard jump is required.

Do not add `duration` and `lerp` arbitrarily. In current Lenis behavior,
duration/easing are superseded when lerp is defined for that animation path.

---

# 22. Reduced motion

Never disable reduced-motion support without a strong reason.

Current Lenis defaults to:

```ts
respectReducedMotion: true
```

When the user has:

```css
@media (prefers-reduced-motion: reduce)
```

Lenis adapts its behavior accordingly.

Other animation systems must also respect reduced motion.

If Lenis is connected to:

- GSAP
- Motion
- WebGL
- parallax
- scroll-linked transforms

then those systems must also reduce/remove non-essential motion.

Do not solve reduced motion by only disabling one animation while leaving a
large amount of scroll-driven movement active elsewhere.

---

# 23. Fixed and sticky elements

Lenis uses native scrolling, so normal sticky and fixed positioning should
remain available.

If a fixed/sticky element lags or behaves incorrectly:

1. test native scrolling without Lenis
2. inspect ancestor transforms
3. inspect ancestor overflow
4. verify the actual scroll container
5. test browser/version
6. test low-power mode
7. test with animation libraries removed

Do not immediately rewrite fixed/sticky elements.

The Lenis project documents an older macOS Safari fixed-position limitation and
Safari frame-rate limitations.

---

# 24. Iframes

The current Lenis CSS disables iframe pointer events while Lenis is in the
smooth-scrolling state.

This exists because iframes do not forward wheel events in a way Lenis can use
reliably.

If an application has:

- YouTube embeds
- maps
- payment providers
- calendars
- editors
- third-party interactive widgets

test iframe interaction specifically.

Do not assume an iframe will behave like a normal div.

If iframe interaction is mission-critical and Lenis creates unacceptable
behavior, consider not using Lenis for that page/experience.

---

# 25. Scroll snap

Lenis does not provide native CSS `scroll-snap` support directly.

Do not combine:

```css
scroll-snap-type
```

with Lenis and assume they will cooperate automatically.

If actual Lenis-compatible snapping is required, investigate the official:

```text
lenis/snap
```

package.

If the page fundamentally depends on browser-native scroll snapping and the
smooth-scroll effect is not needed, native scrolling may be the simpler choice.

---

# 26. Custom scroll containers

For dashboards or special layouts with a scrollable element instead of the
window, do not use `root` blindly.

The application may require:

```tsx
<ReactLenis
  options={{
    wrapper: wrapperElement,
    content: contentElement,
  }}
>
  ...
</ReactLenis>
```

When using a custom wrapper:

- identify the actual scrolling element
- identify the actual content element
- verify dimensions
- verify sticky/fixed behavior
- verify nested scrolling
- verify route transitions
- verify mobile behavior

Do not put `overflow: auto` on a wrapper merely to make Lenis work.

The wrapper must actually match the application's intended scroll container.

---

# 27. Dimensions / dynamic content

Lenis normally uses automatic resizing.

Do not set:

```ts
autoResize: false
```

unless the application has a reason to control resizing itself.

If automatic resize is disabled, call:

```ts
lenis.resize()
```

when content/layout dimensions change.

Important dynamic cases:

- images loading
- fonts changing layout
- accordions expanding
- content streaming in
- route content replacement
- responsive layout changes
- orientation changes

Do not randomly call `resize()` every render.

---

# 28. `naiveDimensions`

Do not enable:

```ts
naiveDimensions: true
```

without understanding why.

The current documentation warns that naive dimension calculation has a
performance impact.

Only use it when its behavior is actually beneficial for the application's
layout.

---

# 29. `allowNestedScroll`

Treat:

```ts
allowNestedScroll: true
```

as a deliberate tradeoff, not a default.

Use it when broad automatic nested-scroll handling is preferable.

Prefer:

```tsx
data-lenis-prevent
```

or targeted variants when only a few known elements need native nested
scrolling.

If nested scrolling becomes slower after enabling it, remove it and switch to
explicit prevention.

---

# 30. `syncTouch`

Do not add:

```ts
syncTouch: true
```

just to "make mobile smoother".

Current Lenis documentation says touch synchronization is a separate behavior
and has compatibility considerations, including older iOS versions.

If enabled:

- test real touch devices
- test scrolling velocity
- test nested scroll
- test interactive controls
- test text selection
- test iOS Safari
- test Android Chrome
- test low-end hardware

If the mobile experience becomes worse, first remove `syncTouch` before adding
other complicated settings.

---

# 31. Infinite scrolling

Do not enable:

```ts
infinite: true
```

unless the product genuinely implements infinite scroll.

It is not a generic "make scrolling feel better" option.

When infinite mode is required, read the current official documentation and
test touch behavior; current Lenis documentation notes that `syncTouch: true`
is required on touch devices for infinite scrolling.

---

# 32. Accessibility requirements

Every Lenis implementation must preserve:

- keyboard scrolling
- native focus movement
- anchor navigation
- semantic HTML
- skip links
- modal focus behavior
- reduced motion
- accessible nested scroll regions

Never replace the native scroll model with a giant translated container just
because an animation tutorial does it.

Prefer Lenis' native-scroll architecture.

---

# 33. Performance rules

Lenis is not a substitute for performance optimization.

When the page contains:

- Three.js / R3F
- WebGL
- many images
- large DOM trees
- heavy SVGs
- expensive GSAP timelines
- expensive React rendering

scrolling may still drop frames.

If mobile drops from a high frame rate to a much lower rate while scrolling:

1. profile the whole animation stack
2. remove Lenis temporarily
3. remove GSAP/ScrollTrigger temporarily
4. remove WebGL/3D temporarily
5. identify the actual bottleneck
6. only then change Lenis configuration

Do not blame Lenis automatically.

Likewise, do not "fix" performance by adding several unrelated Lenis options.

---

# 34. Common wrong fixes

Do NOT:

```text
install an old @studio-freight package
```

Do NOT:

```text
copy old `smoothTouch` examples
```

Do NOT:

```text
use `gestureDirection` when the current version uses `gestureOrientation`
```

Do NOT:

```text
run Lenis autoRaf and GSAP ticker together
```

Do NOT:

```text
create one Lenis instance per component
```

Do NOT:

```text
put every scroll value into React state
```

Do NOT:

```text
enable syncTouch just because the viewport is mobile
```

Do NOT:

```text
enable allowNestedScroll globally without testing
```

Do NOT:

```text
globally set overflow:hidden to hide scroll problems
```

Do NOT:

```text
intercept all link clicks with custom navigation before understanding Next.js
```

Do NOT:

```text
call scrollTo(0) before every route transition without coordinating with the
new route
```

Do NOT:

```text
use random values copied from a portfolio tutorial without measuring the UX
```

Do NOT:

```text
assume every mobile performance issue is caused by Lenis
```

---

# 35. Debugging protocol

When Lenis "does not work":

## A. Prove native scrolling works

Remove Lenis temporarily.

Verify the page/container actually scrolls.

If native scrolling is broken, fix that first.

## B. Verify installation

Check:

```bash
npm ls lenis
```

or the equivalent package-manager command.

Verify that the imported package is:

```ts
lenis
```

not a legacy package.

## C. Verify CSS

Confirm:

```ts
import 'lenis/dist/lenis.css'
```

exists.

## D. Verify RAF

Either:

```ts
autoRaf: true
```

or an actual external:

```ts
lenis.raf(time)
```

clock.

Never neither.

## E. Verify the scroll container

Determine whether the instance uses:

```text
window
document.documentElement
custom wrapper
custom content
```

## F. Test without other animation systems

Temporarily remove:

```text
GSAP
ScrollTrigger
Motion
R3F
Three.js
custom RAF
```

One at a time.

## G. Test nested scroll independently

Remove modals/drawers/nested `overflow:auto` elements.

If the problem disappears, configure prevention rather than globally changing
Lenis.

## H. Test mobile separately

Do not use desktop Chrome device emulation as the only test.

Use real devices when possible.

## I. Test reduced motion

Enable:

```text
prefers-reduced-motion: reduce
```

and verify the experience remains usable.

---

# 36. When stuck: investigation path

If the agent cannot resolve a Lenis issue:

1. Check the installed package version.
2. Read the current Lenis README.
3. Read `packages/react/README.md`.
4. Inspect the actual installed type definitions.
5. Inspect the relevant source in `packages/core/src`.
6. Search current Lenis issues.
7. Search Lenis discussions.
8. Check the release notes for recent fixes.
9. Reproduce with the smallest possible example.
10. Test with Lenis removed.
11. Test with other animation libraries removed.
12. Check the browser-specific behavior in MDN/WebKit/Chrome docs.
13. Only then propose a workaround.

Never invent a Lenis property because an old tutorial uses it.

When a GitHub issue contains a workaround, report it as a workaround, not as
official API behavior, unless the current documentation confirms it.

---

# 37. Version-aware troubleshooting

Older Lenis snippets can be misleading.

Recent releases have changed/fixed behavior around:

- reduced motion
- anchor links
- iOS text selection
- `syncTouch`
- CSS class handling
- `scroll-padding`
- `scroll-margin`
- navigation inertia

Therefore, before using an answer from an old issue:

1. check its date
2. check its package version
3. compare against the current source
4. check whether the issue is closed
5. check recent releases

Do not port a 2023/2024 workaround into a current 2026 installation without
verification.

---

# 38. Official mobile guidance interpretation

Important distinction:

Lenis does NOT mean "replace native mobile touch scrolling with custom smooth
scrolling."

Current Lenis behavior is designed around native scrolling and smooth wheel
handling. Touch synchronization is optional.

Therefore the default strategy for a responsive React/Next.js application is:

```text
Desktop:
  normal Lenis integration
  smooth wheel enabled

Mobile:
  keep the default unless the design specifically needs syncTouch
  preserve native touch behavior where possible
```

Do not automatically create two completely different Lenis architectures for
desktop and mobile.

---

# 39. Production architecture

For a typical Next.js App Router project:

```text
app/
  layout.tsx
  page.tsx
  ...

components/
  smooth-scroll/
    SmoothScroll.tsx

```

`SmoothScroll.tsx`:

```tsx
'use client'

import { ReactLenis } from 'lenis/react'

export function SmoothScroll() {
  return (
    <ReactLenis
      root
      options={{
        autoRaf: true,
        respectReducedMotion: true,
      }}
    />
  )
}
```

`layout.tsx`:

```tsx
import { SmoothScroll } from '@/components/smooth-scroll/SmoothScroll'

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en">
      <body>
        <SmoothScroll />
        {children}
      </body>
    </html>
  )
}
```

Do not add additional options unless required.

If GSAP owns the clock, change the architecture to the official GSAP pattern
instead of adding a second RAF.

---

# 40. Completion criteria

A Lenis implementation is not complete just because scrolling feels smooth.

Verify:

```text
[ ] Correct current `lenis` package
[ ] No legacy @studio-freight package
[ ] Correct React adapter: `lenis/react`
[ ] Browser-only setup is inside a Client Component
[ ] Root provider is stable across routes
[ ] Official Lenis CSS imported
[ ] Exactly one animation clock
[ ] No duplicate Lenis instances
[ ] No competing smooth-scroll library
[ ] Native scrolling works without Lenis
[ ] Desktop mouse/trackpad tested
[ ] Mobile Safari tested
[ ] Android Chrome tested
[ ] Nested scroll tested
[ ] Modal/drawer scroll tested
[ ] Anchor links tested
[ ] Route navigation tested while scrolling
[ ] Reduced motion tested
[ ] Sticky/fixed elements tested
[ ] iframe interaction tested when applicable
[ ] Scroll snap requirements checked
[ ] No excessive React renders
[ ] Production build passes
[ ] No runtime warnings/errors
```

Only declare the implementation complete after the relevant checks pass.

---

# 41. Non-negotiable agent behavior

When asked:

> "Add Lenis"

Do NOT immediately write code.

First inspect the project.

When asked:

> "Make scrolling smoother on mobile"

Do NOT immediately enable `syncTouch`.

First determine whether native touch behavior is already adequate and whether
the issue is actually caused by Lenis, WebGL, GSAP, layout, or browser
performance.

When asked:

> "Fix Lenis navigation"

Do NOT immediately add `scrollTo(0)` to every page.

Inspect Next.js navigation, Lenis inertia, route lifecycle, and `<Link>`
scroll behavior first.

When asked:

> "Lenis is broken"

Do NOT randomly change:

```text
duration
lerp
easing
wheelMultiplier
touchMultiplier
syncTouch
```

Find the actual failure first.

When unsure:

> stop coding -> verify current docs/source/version -> reproduce -> isolate ->
> implement -> test.

---

# 42. Reference URLs

Primary:

- https://github.com/darkroomengineering/lenis
- https://github.com/darkroomengineering/lenis/blob/main/README.md
- https://github.com/darkroomengineering/lenis/blob/main/packages/react/README.md
- https://github.com/darkroomengineering/lenis/blob/main/packages/core/lenis.css
- https://github.com/darkroomengineering/lenis/releases
- https://github.com/darkroomengineering/lenis/issues
- https://github.com/darkroomengineering/lenis/discussions

React/Next.js:

- https://nextjs.org/docs/app/api-reference/directives/use-client
- https://nextjs.org/docs/app/api-reference/components/link
- https://nextjs.org/docs/app/getting-started/server-and-client-components

Accessibility/browser behavior:

- https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-motion

---

# 43. Final rule

The agent's job is NOT:

"make Lenis work somehow."

The job is:

"Integrate the current Lenis API into the existing React/Next.js architecture
with the smallest correct configuration, preserving native scrolling,
accessibility, mobile behavior, route navigation, nested scrolling, and
performance."

Prefer a boring, correct Lenis integration over a complex, visually impressive
implementation copied from an outdated tutorial.

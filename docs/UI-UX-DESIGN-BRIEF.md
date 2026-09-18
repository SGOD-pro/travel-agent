# UI/UX design brief

Version 1.0 | Change when user journeys, interaction or visual requirements change.

## Product feel

A calm, precise travel workspace with one clear next action. Avoid excessive agent labels, animated reasoning feeds, false certainty and overloaded dashboards. The brand is not yet visually finalized; do not impose the unrelated auth dashboard's car theme. Final tokens/typefaces require a design review; start with accessible semantic tokens and a consistent spacing scale.

## Information architecture

Trip list -> new trip/brief editor -> planning workspace -> saved itinerary -> export or official booking handoff. Settings contains profile, consent, preferences and deletion. Authentication uses the approved identity boundary. Empty, loading, partial, stale, failed and completed states are first-class.

Desktop workspace: editable day timeline, synchronized map and contextual details. Mobile: timeline/map tabs with a bottom sheet for selected place/offer. Preserve selection, filters and scroll position across switches. A map error must leave a usable list itinerary.

## Core flows

1. Brief: ask origin, dates/flexible period, travelers, budget and modes; reveal rooms/child ages only when applicable. Fixed choices appear as visible locked constraints. Ask whether an ambiguous bike means motorcycle or bicycle; EV modeling is unavailable.
2. Generation: acknowledge immediately, show route skeleton if ready, then independent sections. Do not use fictitious percentage progress. Explain which data is missing.
3. Editing: user can pin/remove/reorder stops, change hotel or budget. Show changed travel time, budget, affected constraints and stale sections before accepting a replan. Keep undo through versioned state.
4. Recommendations: show why it fits, detour time, visit duration, source, access/hour uncertainty and provider attribution. Hidden gem and must visit are explained categories, not guarantees.
5. Handoff: merchant name/domain, requested dates/party, room/fare type, observed price and included/excluded fees. Button: “View on [merchant]”. If exact context cannot be preserved say “Search on [merchant]”. No “booking confirmed” after redirect.

## Evidence presentation

| Internal classification | User copy | Behavior |
| --- | --- | --- |
| LIVE_OFFER | Current provider offer; checked [time] | Show expiry if supplied; not a price hold |
| INDICATIVE_SEARCH | Observed price; verify on provider | Explain missing context/inclusions |
| EDITORIAL_DISCOVERY | Source information | Type-specific label for place/route/seasonal reference |
| UNAVAILABLE | Could not verify | Reason and retry/alternative action |

Use text and icon in addition to color. Show freshness near the fact rather than buried in a tooltip. Server evidence class controls the UI; no client-side promotion.

## Budget component

Show transport, hotels, food, attractions, fuel, tolls, parking, taxes/fees and contingency separately. Distinguish confirmed provider observations, estimates, user assumptions and unknown values. Fuel/mileage inputs are editable with units. A total missing tolls says “Known/estimated subtotal; tolls unknown”, not “Within budget”. Per-person total explains room/vehicle sharing and excludes infants only when source rules support it.

## Accessibility and performance acceptance

Keyboard-complete forms/timeline/handoff; visible focus; labeled validation; accessible contrast; reduced motion; no essential hover-only controls. Map interactions have list equivalents. Test representative mobile, tablet and desktop widths, long Indian place names, INR formatting and slow networks. Avoid map reloads on unrelated edits, lazy-load heavy views, and preserve form state during auth refresh. Visual regression checks cover normal, partial, empty and error states; “pixel perfect” applies to our UI, not merchant websites.

## Deferred design work

Voice input/output and multilingual parsing remain later modules. Competitive journey review is pending: evaluate planning edits, evidence transparency, booking handoff and mobile usability with dated firsthand observations. Do not invent competitor weaknesses from the original PDF.

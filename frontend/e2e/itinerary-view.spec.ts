import { test, expect } from "@playwright/test";

/**
 * Mocked UI Acceptance Test Suite for Itinerary View (Spec 0007 Slice 6).
 *
 * Uses Playwright route interception (page.route) to verify client presentation,
 * badge styling, and UI state transitions against deterministic fixture payloads.
 * (Live database and real cryptographic auth paths are verified in itinerary-live-backend.spec.ts).
 * 1. Loaded proposal state with schedule timeline, interactive map, weather advisory, and budget lines.
 * 2. Operating hours invariant: stops with assumed hours display the HOURS_ASSUMED badge (AC-3).
 * 3. Zero-coercion guarantee: unquoted tolls remain UNKNOWN and are not coerced to zero (AC-7).
 * 4. Candidate provenance: editorial discoveries display EDITORIAL_DISCOVERY uncertainty tags (AC-3).
 * 5. Planning in progress: HTTP 409 displays the dedicated in-progress banner and retry trigger.
 * 6. Tenant isolation: HTTP 403 displays the access forbidden card.
 * 7. Not found: HTTP 404 displays the proposal not found card.
 * 8. Resilient partial outcome: routing engine unavailable displays honest diagnostic notices without fabricated road geometry (AC-9).
 * 9. Weather attribution: Open Meteo attribution notice is displayed (AC-6).
 */

const SAMPLE_TRIP_ID = "11111111-2222-3333-4444-555555555555";

const MOCK_PROPOSAL = {
  trip_id: SAMPLE_TRIP_ID,
  version: 1,
  state: "proposed",
  status: "ready_for_approval",
  schedule: {
    stops: [
      {
        position: 1,
        name: "Bengaluru MG Road",
        latitude: 12.9716,
        longitude: 77.5946,
        arrival_time: "2026-11-14T09:00:00Z",
        departure_time: "2026-11-14T09:30:00Z",
        stay_duration_minutes: 30,
        is_hours_assumed: false,
        hours_status: "CONFIRMED",
      },
      {
        position: 2,
        name: "Mysuru Palace Heritage Zone",
        latitude: 12.3051,
        longitude: 76.6551,
        arrival_time: "2026-11-14T12:30:00Z",
        departure_time: "2026-11-14T16:30:00Z",
        stay_duration_minutes: 240,
        is_hours_assumed: true,
        hours_status: "HOURS_ASSUMED",
      },
    ],
    legs: [
      {
        from_stop: "Bengaluru MG Road",
        to_stop: "Mysuru Palace Heritage Zone",
        duration_minutes: 180,
        distance_km: 145.0,
      },
    ],
    total_travel_minutes: 180,
    is_optimal: true,
  },
  budget_summary: {
    known_total: { amount: "4750.00", currency: "INR" },
    is_complete: false,
    unknown_count: 1,
    unknown_categories: ["tolls_and_fees"],
  },
  budget_lines: [
    {
      category: "transportation",
      amount: "3750.00",
      currency: "INR",
      basis: "fuel_estimate_car_petrol",
      quantity: "1",
      estimated: true,
      unknown_reason: null,
    },
    {
      category: "activities",
      amount: "1000.00",
      currency: "INR",
      basis: "entry_fee_mysuru_palace",
      quantity: "1",
      estimated: false,
      unknown_reason: null,
    },
    {
      category: "tolls_and_fees",
      amount: null,
      currency: "INR",
      basis: "nh275_expressway_toll",
      quantity: "1",
      estimated: false,
      unknown_reason: "Toll rates unverified for NH 275 expressway",
    },
  ],
  editorial_recommendations: [
    {
      name: "Ranganathittu Bird Sanctuary",
      category: "nature",
      fit_score: 0.94,
      uncertainty: "EDITORIAL_DISCOVERY",
      description: "Nesting waterfowl sanctuary along Kaveri river",
      source: "KSTDC Tourism Portal",
    },
  ],
  excluded_candidates: [],
  weather_advisories: [
    {
      stop_name: "Bengaluru MG Road",
      latitude: 12.9716,
      longitude: 77.5946,
      temperature_celsius: 24.5,
      condition: "sunny",
      humidity_percent: 55,
      monsoon_risk: "low",
    },
    {
      stop_name: "Mysuru Palace Heritage Zone",
      latitude: 12.3051,
      longitude: 76.6551,
      temperature_celsius: 27.2,
      condition: "cloudy",
      humidity_percent: 65,
      monsoon_risk: "low",
    },
  ],
  weather_status: "AVAILABLE",
  validated_candidates: [],
  discovered_places: [],
  route_id: "77777777-7777-7777-7777-777777777777",
  route_status: "FEASIBLE",
  routing_status: "OPTIMAL",
  route_diagnostics: [],
  provider_diagnostics: {
    weather: { provider: "open_meteo", horizon_days: 16 },
  },
  map_geojson: {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        geometry: { type: "Point", coordinates: [77.5946, 12.9716] },
        properties: { feature_type: "stop", name: "Bengaluru MG Road", stop_order: 1 },
      },
      {
        type: "Feature",
        geometry: { type: "Point", coordinates: [76.6551, 12.3051] },
        properties: { feature_type: "stop", name: "Mysuru Palace Heritage Zone", stop_order: 2 },
      },
    ],
  },
  generated_at: new Date().toISOString(),
};

const MOCK_TRIP = {
  id: SAMPLE_TRIP_ID,
  owner_id: "00000000-0000-0000-0000-000000000001",
  current_version: 1,
  state: "proposed",
  status: "proposed",
  brief: {
    origin_name: "Bengaluru",
    origin_lat: 12.9716,
    origin_lng: 77.5946,
    destinations: [
      { name: "Mysuru Palace", latitude: 12.3051, longitude: 76.6551, stay_days: 1 },
    ],
    start_date: "2026-11-14",
    end_date: "2026-11-16",
    timezone: "Asia/Kolkata",
    preferred_modes: ["car_petrol"],
  },
  current_route_id: "77777777-7777-7777-7777-777777777777",
};

test.describe("Itinerary Proposal Mocked UI Acceptance (Spec 0007 Slice 6)", () => {
  test("Scenario 1: Renders complete published itinerary with schedule, weather, map, and zero-coercion budget", async ({
    page,
  }) => {
    // Intercept backend API endpoints
    await page.route(`**/api/v1/trips/${SAMPLE_TRIP_ID}/itinerary`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(MOCK_PROPOSAL),
      });
    });

    await page.route(`**/api/v1/trips/${SAMPLE_TRIP_ID}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(MOCK_TRIP),
      });
    });

    await page.goto(`/trips/${SAMPLE_TRIP_ID}`);

    // Verify Title and Subtitle
    await expect(page.locator("h1")).toContainText("Bengaluru to Mysuru Palace");

    // Verify AC-3: HOURS_ASSUMED badge
    const assumedBadge = page.locator("span", { hasText: "HOURS_ASSUMED" });
    await expect(assumedBadge).toBeVisible();

    // Verify AC-7: Zero-coercion guarantee in budget breakdown
    const unknownBadge = page.locator("span", { hasText: "UNKNOWN" }).first();
    await expect(unknownBadge).toBeVisible();
    await expect(page.locator("body")).toContainText("Toll rates unverified for NH 275 expressway");
    await expect(page.locator("body")).toContainText("Zero-coercion guarantee");

    // Verify AC-3 / AC-11: Editorial discovery uncertainty tag
    const editorialBadge = page.locator("span", { hasText: "EDITORIAL_DISCOVERY" });
    await expect(editorialBadge).toBeVisible();
    await expect(page.locator("body")).toContainText("Ranganathittu Bird Sanctuary");

    // Verify AC-6: Open Meteo attribution
    await expect(page.locator("body")).toContainText("Weather data by Open Meteo.com");
  });

  test("Scenario 2: Renders planning in progress (409 Conflict) state", async ({ page }) => {
    await page.route(`**/api/v1/trips/${SAMPLE_TRIP_ID}/itinerary`, async (route) => {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Trip planning is in progress" }),
      });
    });

    await page.goto(`/trips/${SAMPLE_TRIP_ID}`);

    await expect(page.locator("h2")).toContainText("Trip Planning in Progress");
    await expect(page.locator("button", { hasText: "Check Status Now" })).toBeVisible();
  });

  test("Scenario 3: Renders access forbidden (403 Forbidden) state on cross-tenant access", async ({
    page,
  }) => {
    await page.route(`**/api/v1/trips/${SAMPLE_TRIP_ID}/itinerary`, async (route) => {
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Access forbidden: trip belongs to another tenant" }),
      });
    });

    await page.goto(`/trips/${SAMPLE_TRIP_ID}`);

    await expect(page.locator("h2")).toContainText("Access Forbidden");
    await expect(page.locator("body")).toContainText("Strict tenant isolation protects this travel proposal");
  });

  test("Scenario 4: Renders proposal not found (404 Not Found) state", async ({ page }) => {
    await page.route(`**/api/v1/trips/${SAMPLE_TRIP_ID}/itinerary`, async (route) => {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Itinerary not found. No proposal has been published." }),
      });
    });

    await page.goto(`/trips/${SAMPLE_TRIP_ID}`);

    await expect(page.locator("h2")).toContainText("Itinerary Proposal Not Found");
    await expect(page.locator("a", { hasText: "Go to Dashboard" })).toBeVisible();
  });

  test("Scenario 5: Renders unavailable routing diagnostic without fabricating road polylines", async ({
    page,
  }) => {
    const unavailableProposal = {
      ...MOCK_PROPOSAL,
      route_id: null,
      route_status: "UNAVAILABLE",
      routing_status: "ROUTING_UNAVAILABLE",
      map_geojson: null,
      route_diagnostics: [
        "Road routing engine unavailable; stops preserved without driving schedule or route polyline",
      ],
    };

    await page.route(`**/api/v1/trips/${SAMPLE_TRIP_ID}/itinerary`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(unavailableProposal),
      });
    });

    await page.route(`**/api/v1/trips/${SAMPLE_TRIP_ID}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(MOCK_TRIP),
      });
    });

    await page.goto(`/trips/${SAMPLE_TRIP_ID}`);

    await expect(
      page.getByRole("heading", { name: "Road Routing Engine Unavailable" })
    ).toBeVisible();
    await expect(page.locator("body")).toContainText(
      "In conformance with our fail-closed routing guarantee, straight-line distance guesses or fabricated travel times are never generated"
    );
  });
});

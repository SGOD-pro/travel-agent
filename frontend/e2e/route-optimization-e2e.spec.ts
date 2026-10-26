import { test, expect } from "@playwright/test";

/**
 * End-to-end browser test suite for Route Optimization, CorridorMap GeoJSON layer,
 * safe popups, mid-trip replanning, and version lineage (conforming to spec 0006 AC-10, AC-11).
 */

const SAMPLE_TRIP_ID = "00000000-0000-0000-0000-000000000001";
const BASE_ROUTE_ID = "11111111-1111-1111-1111-111111111111";
const REPLAN_ROUTE_ID = "22222222-2222-2222-2222-222222222222";
const INFEASIBLE_ROUTE_ID = "33333333-3333-3333-3333-333333333333";

// Authoritative GeoJSON response fixture for a multi-modal feasible route
const mockFeasibleMapGeoJson = {
  type: "FeatureCollection",
  bbox: [76.64, 12.3, 77.6, 12.98],
  properties: {
    route_id: BASE_ROUTE_ID,
    route_version: 1,
    parent_route_id: null,
    status: "FEASIBLE",
    solver_status: "OPTIMAL",
    total_distance_m: 142500,
    total_travel_seconds: 9300,
    fuel_cost: "1250",
    warnings: [],
    diagnostics: [],
  },
  features: [
    // 1. Restriction Polygon (Avoidance area)
    {
      type: "Feature",
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [76.85, 12.55],
            [76.95, 12.55],
            [76.95, 12.65],
            [76.85, 12.65],
            [76.85, 12.55],
          ],
        ],
      },
      properties: {
        feature_type: "restriction",
        restriction_type: "ROAD_CLOSURE",
        fill: "#ef4444",
        "fill-opacity": 0.35,
        stroke: "#b91c1c",
        description: "NH-275 Bridge repair and active road closure",
      },
    },
    // 2. Driving Leg
    {
      type: "Feature",
      geometry: {
        type: "LineString",
        coordinates: [
          [77.5946, 12.9716],
          [77.1, 12.7],
          [76.6551, 12.3051],
        ],
      },
      properties: {
        feature_type: "leg",
        leg_order: 1,
        from_stop_id: "stop-origin",
        to_stop_id: "stop-parking",
        transport_mode: "DRIVE",
        distance_m: 140000,
        travel_seconds: 9000,
        traffic_basis: "HISTORICAL_SPEEDS",
        stroke: "#3b82f6",
        "stroke-width": 4,
        "stroke-dasharray": "none",
      },
    },
    // 3. Walking Leg
    {
      type: "Feature",
      geometry: {
        type: "LineString",
        coordinates: [
          [76.6551, 12.3051],
          [76.653, 12.304],
        ],
      },
      properties: {
        feature_type: "leg",
        leg_order: 2,
        from_stop_id: "stop-parking",
        to_stop_id: "stop-palace",
        transport_mode: "WALK",
        distance_m: 2500,
        travel_seconds: 300,
        traffic_basis: "PEDESTRIAN_NETWORK",
        stroke: "#10b981",
        "stroke-width": 3,
        "stroke-dasharray": "5, 5",
      },
    },
    // 4. Stop: Origin
    {
      type: "Feature",
      geometry: {
        type: "Point",
        coordinates: [77.5946, 12.9716],
      },
      properties: {
        feature_type: "stop",
        id: "stop-origin",
        stop_order: 1,
        name: "Bengaluru MG Road Origin",
        stop_type: "ORIGIN",
        planned_arrival_at: "2026-10-27T08:00:00Z",
        planned_departure_at: "2026-10-27T08:00:00Z",
        dwell_seconds: 0,
        wait_seconds: 0,
        is_completed: true,
        is_skipped: false,
        skip_reason: null,
        "marker-color": "#2563eb",
        "marker-symbol": "1",
      },
    },
    // 5. Stop: Parking Hub
    {
      type: "Feature",
      geometry: {
        type: "Point",
        coordinates: [76.6551, 12.3051],
      },
      properties: {
        feature_type: "stop",
        id: "stop-parking",
        stop_order: 2,
        name: "Mysuru Town Hall Parking Hub",
        stop_type: "PARKING_HUB",
        planned_arrival_at: "2026-10-27T10:30:00Z",
        planned_departure_at: "2026-10-27T10:45:00Z",
        dwell_seconds: 900,
        wait_seconds: 0,
        is_completed: false,
        is_skipped: false,
        skip_reason: null,
        "marker-color": "#d97706",
        "marker-symbol": "P",
      },
    },
    // 6. Stop: Visited Venue with XSS Injection Payload to test safe escaping
    {
      type: "Feature",
      geometry: {
        type: "Point",
        coordinates: [76.653, 12.304],
      },
      properties: {
        feature_type: "stop",
        id: "stop-palace",
        stop_order: 3,
        name: '<b id="xss-test">Mysuru Palace Heritage</b><script>window.__xss_vulnerable=true;</script>',
        stop_type: "VENUE",
        planned_arrival_at: "2026-10-27T10:50:00Z",
        planned_departure_at: "2026-10-27T12:50:00Z",
        dwell_seconds: 7200,
        wait_seconds: 0,
        is_completed: false,
        is_skipped: false,
        skip_reason: null,
        "marker-color": "#059669",
        "marker-symbol": "3",
      },
    },
  ],
};

// Versions history fixture
const mockVersionHistory = {
  root_route_id: BASE_ROUTE_ID,
  current_route_id: BASE_ROUTE_ID,
  versions: [
    {
      route_id: BASE_ROUTE_ID,
      route_version: 1,
      parent_route_id: null,
      created_at: "2026-10-27T08:00:00Z",
      status: "FEASIBLE",
      total_distance_m: 142500,
      total_travel_seconds: 9300,
      distance_delta_m: 0,
      duration_delta_seconds: 0,
      dropped_stop_ids: [],
      replan_origin: null,
    },
    {
      route_id: REPLAN_ROUTE_ID,
      route_version: 2,
      parent_route_id: BASE_ROUTE_ID,
      created_at: "2026-10-27T09:15:00Z",
      status: "FEASIBLE",
      total_distance_m: 154900,
      total_travel_seconds: 10200,
      distance_delta_m: 12400,
      duration_delta_seconds: 900,
      dropped_stop_ids: [],
      replan_origin: [76.645, 12.315],
    },
  ],
};

// Infeasible route fixture
const mockInfeasibleMapGeoJson = {
  type: "FeatureCollection",
  bbox: [76.6, 12.3, 77.6, 12.98],
  properties: {
    route_id: INFEASIBLE_ROUTE_ID,
    route_version: 1,
    parent_route_id: null,
    status: "INFEASIBLE",
    solver_status: "INFEASIBLE",
    total_distance_m: 0,
    total_travel_seconds: 0,
    fuel_cost: null,
    warnings: [],
    diagnostics: [
      "Mandatory venue 'Mysuru Heritage Museum' closes at 11:00, earliest arrival computed at 12:45.",
      "Time window violation of 6300 seconds under active speed profile.",
    ],
  },
  features: [
    {
      type: "Feature",
      geometry: {
        type: "Point",
        coordinates: [77.5946, 12.9716],
      },
      properties: {
        feature_type: "stop",
        id: "stop-infeasible-1",
        stop_order: 1,
        name: "Bengaluru Origin",
        stop_type: "ORIGIN",
        planned_arrival_at: null,
        planned_departure_at: null,
        dwell_seconds: 0,
        wait_seconds: 0,
        is_completed: false,
        is_skipped: false,
        skip_reason: null,
        "marker-color": "#2563eb",
        "marker-symbol": "1",
      },
    },
    {
      type: "Feature",
      geometry: {
        type: "Point",
        coordinates: [76.653, 12.304],
      },
      properties: {
        feature_type: "stop",
        id: "stop-infeasible-2",
        stop_order: 2,
        name: "Mysuru Heritage Museum",
        stop_type: "DESTINATION",
        planned_arrival_at: null,
        planned_departure_at: null,
        dwell_seconds: 3600,
        wait_seconds: 0,
        is_completed: false,
        is_skipped: true,
        skip_reason: "MANDATORY_TIME_WINDOW_VIOLATION",
        "marker-color": "#dc2626",
        "marker-symbol": "2",
      },
    },
  ],
};

test.describe("Route Optimization and CorridorMap E2E (Spec 0006)", () => {
  test.beforeEach(async ({ page }) => {
    // Intercept backend API calls with deterministic mock responses
    await page.route("**/api/v1/optimization/routes", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({
            job_id: "test-job-e2e-001",
            status_url: "/api/v1/jobs/test-job-e2e-001",
          }),
        });
        return;
      }
      await route.continue();
    });

    await page.route(`**/api/v1/optimization/routes/${BASE_ROUTE_ID}/map*`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockFeasibleMapGeoJson),
      });
    });

    await page.route(`**/api/v1/optimization/routes/${BASE_ROUTE_ID}/versions`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockVersionHistory),
      });
    });

    await page.route(`**/api/v1/optimization/routes/${INFEASIBLE_ROUTE_ID}/map*`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockInfeasibleMapGeoJson),
      });
    });

    await page.route(`**/api/v1/optimization/routes/${INFEASIBLE_ROUTE_ID}/versions`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          root_route_id: INFEASIBLE_ROUTE_ID,
          current_route_id: INFEASIBLE_ROUTE_ID,
          versions: [],
        }),
      });
    });
  });

  test("1. Renders Route Optimizer layout, breadcrumb link, and page elements", async ({ page }) => {
    await page.goto(`/trips/${SAMPLE_TRIP_ID}/route`);

    // Verify main header
    const heading = page.locator("h1");
    await expect(heading).toBeVisible();
    await expect(heading).toContainText("Route Optimizer & Geospatial Map");

    // Verify back navigation link to Trip details
    const backLink = page.getByRole("link", { name: "Back to Trip Details" });
    await expect(backLink).toBeVisible();
    await expect(backLink).toHaveAttribute("href", `/trips/${SAMPLE_TRIP_ID}`);

    // Verify initial metrics card
    const metricsCard = page.getByRole("heading", { name: "Trip Execution Metrics" });
    await expect(metricsCard).toBeVisible();

    // Verify mid-trip replan panel
    const replanHeading = page.getByRole("heading", { name: "Mid-Trip Dynamic Replan" });
    await expect(replanHeading).toBeVisible();
  });

  test("2. Displays polling progress banner and status transitions during solver execution", async ({ page }) => {
    // Intercept active job polling
    let pollCount = 0;
    await page.route("**/api/v1/jobs/test-active-job-999", async (route) => {
      pollCount += 1;
      if (pollCount === 1) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            job_id: "test-active-job-999",
            status: "RUNNING",
            workflow: "ROUTE_OPTIMIZER_GRAPH",
            result_available: false,
            result_ref: null,
            error: null,
          }),
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            job_id: "test-active-job-999",
            status: "SUCCEEDED",
            workflow: "ROUTE_OPTIMIZER_GRAPH",
            result_available: true,
            result_ref: { route_id: BASE_ROUTE_ID },
            error: null,
          }),
        });
      }
    });

    // Navigate with active job_id in URL query
    await page.goto(`/trips/${SAMPLE_TRIP_ID}/route?job_id=test-active-job-999`);

    // Verify polling banner is rendered
    const pollingBanner = page.locator("text=Solving Vehicle Routing Problem with Time Windows...");
    await expect(pollingBanner).toBeVisible();
    await expect(page.locator("text=test-active-job-999")).toBeVisible();

    // Verify transition to SUCCEEDED and map display
    await expect(page.locator("text=FEASIBLE (v1)")).toBeVisible({ timeout: 10000 });
  });

  test("3. Consumes GeoJSON FeatureCollection rendering multi-color paths, pins, metrics, and attribution (AC-10)", async ({ page }) => {
    await page.goto(`/trips/${SAMPLE_TRIP_ID}/route?route_id=${BASE_ROUTE_ID}`);

    // Verify status badge and metrics on trip card
    await expect(page.locator("text=FEASIBLE (v1)").first()).toBeVisible();
    await expect(page.locator("text=142.5 km").first()).toBeVisible();
    await expect(page.locator("text=2h 35m").first()).toBeVisible();
    await expect(page.locator("text=₹1250").first()).toBeVisible();

    // Verify top-left floating overlay on CorridorMap
    const mapContainer = page.locator(".leaflet-container");
    await expect(mapContainer).toBeVisible();

    // Verify tile layer attribution (strictly required by AC-10)
    const attribution = page.locator(".leaflet-control-attribution");
    await expect(attribution).toBeVisible();
    await expect(attribution).toContainText("OpenStreetMap");
    await expect(attribution).toContainText("CARTO");

    // Verify custom markers for visit order and parking
    const stopMarkers = page.locator(".custom-geojson-stop-marker");
    await expect(stopMarkers).toHaveCount(3);

    // Verify Stop 1 (Origin)
    await expect(stopMarkers.nth(0)).toContainText("1");
    // Verify Stop 2 (Parking Hub with 'P' symbol)
    await expect(stopMarkers.nth(1)).toContainText("P");
    // Verify Stop 3 (Venue)
    await expect(stopMarkers.nth(2)).toContainText("3");

    // Verify legend
    await expect(page.getByText("Drive", { exact: true })).toBeVisible();
    await expect(page.getByText("Walk", { exact: true })).toBeVisible();
    await expect(page.getByText("Parking", { exact: true })).toBeVisible();
  });

  test("4. Safe popup rendering protects against Cross-Site Scripting (XSS)", async ({ page }) => {
    await page.goto(`/trips/${SAMPLE_TRIP_ID}/route?route_id=${BASE_ROUTE_ID}`);

    // Click on the third marker (which has malicious payload in its venue name)
    const venueMarker = page.locator(".custom-geojson-stop-marker").nth(2);
    await venueMarker.click();

    // Verify popup is open and rendered
    const popup = page.locator(".swena-safe-stop-popup");
    await expect(popup).toBeVisible();

    // Verify textContent escaping: the script tags must NOT execute
    const isVulnerable = await page.evaluate(() => {
      return (window as any).__xss_vulnerable === true;
    });
    expect(isVulnerable).toBe(false);

    // Verify that the HTML was not parsed as elements inside the popup
    const rawTagCount = await popup.locator("#xss-test").count();
    expect(rawTagCount).toBe(0);

    // Verify plain text displays safely
    await expect(popup).toContainText("Mysuru Palace Heritage");
  });

  test("5. Mid-trip replanning form submission with completed stops checklist", async ({ page }) => {
    // Intercept replan submission endpoint
    await page.route(`**/api/v1/optimization/routes/${BASE_ROUTE_ID}/replans`, async (route) => {
      const payload = JSON.parse(route.request().postData() || "{}");
      expect(payload.current_location.latitude).toBeCloseTo(12.315, 2);
      expect(payload.current_location.longitude).toBeCloseTo(76.645, 2);
      expect(payload.completed_stop_ids).toContain("stop-origin");

      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          job_id: "job-replan-detour-555",
          status_url: "/api/v1/jobs/job-replan-detour-555",
        }),
      });
    });

    await page.goto(`/trips/${SAMPLE_TRIP_ID}/route?route_id=${BASE_ROUTE_ID}`);

    // Verify completed stops checklist populated with stops from routeMap
    const stopCheckItem = page.locator("text=1. Bengaluru MG Road Origin");
    await expect(stopCheckItem).toBeVisible();

    // Check off Stop 1 as completed
    await stopCheckItem.click();

    // Submit replan form
    const replanBtn = page.getByRole("button", { name: /Replan & Detour/i });
    await expect(replanBtn).toBeEnabled();
    await replanBtn.click();

    // Verify replan tracking message
    const flashMsg = page.locator("text=Replan initiated. Tracking job job-replan-detour-555...");
    await expect(flashMsg).toBeVisible();
  });

  test("6. Version lineage dropdown selection updates active route view (AC-7)", async ({ page }) => {
    await page.goto(`/trips/${SAMPLE_TRIP_ID}/route?route_id=${BASE_ROUTE_ID}`);

    // Verify version selector dropdown exists with both versions
    const versionSelect = page.locator("#version-select");
    await expect(versionSelect).toBeVisible();

    const options = versionSelect.locator("option");
    await expect(options).toHaveCount(2);
    await expect(options.nth(0)).toContainText("v1 (Base) • FEASIBLE");
    await expect(options.nth(1)).toContainText("v2 (+12.4km) • FEASIBLE");

    // Switching version triggers navigation to that version
    await versionSelect.selectOption(REPLAN_ROUTE_ID);
    await expect(page).toHaveURL(new RegExp(`route_id=${REPLAN_ROUTE_ID}`));
  });

  test("7. Infeasible route renders failure pins, skip reasons, and bottom diagnostics banner (AC-8)", async ({ page }) => {
    await page.goto(`/trips/${SAMPLE_TRIP_ID}/route?route_id=${INFEASIBLE_ROUTE_ID}`);

    // Status badge reflects INFEASIBLE with red styling
    const infeasibleBadge = page.locator("text=INFEASIBLE (v1)");
    await expect(infeasibleBadge).toBeVisible();

    // Verify bottom diagnostics banner is rendered with constraint message
    const noticeHeader = page.locator("text=Notice:");
    await expect(noticeHeader).toBeVisible();
    await expect(
      page.locator("text=Mandatory venue 'Mysuru Heritage Museum' closes at 11:00").first()
    ).toBeVisible();

    // Verify right column diagnostics box
    await expect(
      page.locator("text=Mathematical Hard Constraint Infeasibility")
    ).toBeVisible();

    // Verify stop pin 2 has red skipped styling
    const failureMarker = page.locator(".custom-geojson-stop-marker").nth(1);
    await expect(failureMarker).toContainText("2");

    // Click marker and verify skip reason popup
    await failureMarker.click();
    const popup = page.locator(".swena-safe-stop-popup");
    await expect(popup).toBeVisible();
    await expect(popup).toContainText("SKIPPED");
    await expect(popup).toContainText("Skip Reason: MANDATORY_TIME_WINDOW_VIOLATION");
  });

  test("8. Clean browser execution without uncaught page errors or exceptions", async ({ page }) => {
    const errorLogs: string[] = [];
    page.on("pageerror", (err) => errorLogs.push(err.message));

    await page.goto(`/trips/${SAMPLE_TRIP_ID}/route?route_id=${BASE_ROUTE_ID}`);
    await page.waitForTimeout(1000);

    expect(errorLogs).toEqual([]);
  });
});

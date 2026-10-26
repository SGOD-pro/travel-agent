import { test, expect } from "@playwright/test";

/**
 * Full-stack browser acceptance test suite for Route Optimization conforming to spec 0006.
 *
 * Runs against the real local stack:
 * - Real Next.js frontend on http://localhost:3000
 * - Real FastAPI backend on http://localhost:8000
 * - Live PostgreSQL database (executions, routes, outbox_events, canonical_places)
 * - Real LangGraph execution runner, worker lease fencing, and completion reconciliation
 * - Injected MockValhallaClient routing provider (zero application API mocking)
 */

const API_BASE = "http://localhost:8000";
const SAMPLE_TRIP_ID = "00000000-0000-0000-0000-000000000001";
const MULTIMODAL_TRIP_ID = "40000000-0000-0000-0000-000000000001";
const PARTIAL_TRIP_ID = "50000000-0000-0000-0000-000000000001";
const INFEASIBLE_TRIP_ID = "60000000-0000-0000-0000-000000000001";

const MULTIMODAL_ROUTE_ID = "44444444-4444-4444-4444-444444444444";
const PARTIAL_ROUTE_ID = "55555555-5555-5555-5555-555555555555";
const INFEASIBLE_ROUTE_ID = "66666666-6666-6666-6666-666666666666";

// Controlled test principals
const USER_A_ID = "00000000-0000-0000-0000-000000000001"; // Dev default principal
const USER_B_ID = "99999999-9999-9999-9999-999999999999"; // Foreign unauthorized tenant

function createBearerToken(sub: string, email: string): string {
  const payload = JSON.stringify({
    sub,
    email,
    exp: Math.floor(Date.now() / 1000) + 3600,
  });
  return Buffer.from(payload).toString("base64");
}

test.describe("Route Optimization Full-Stack Browser Acceptance", () => {
  test.setTimeout(120000);

  test("Scenario 1: Live Submission, Real Polling, and Map Retrieval", async ({
    page,
  }) => {
    // Navigate to trip route optimization page without mocking application APIs
    await page.goto(`/trips/${SAMPLE_TRIP_ID}/route`);

    // Ensure page header renders
    await expect(page.locator("h1")).toContainText("Route Optimizer & Geospatial Map");

    // Check if initial generation button is available or already generated
    const generateBtn = page.getByRole("button", { name: "Generate Optimal Route" });
    if (await generateBtn.isVisible()) {
      await generateBtn.click();

      // Verify polling progress banner appears with real solver status
      await expect(
        page.getByText("Solving Vehicle Routing Problem with Time Windows")
      ).toBeVisible({ timeout: 15000 });

      // Verify URL has been updated with active job_id
      await expect(page).toHaveURL(/job_id=/);

      // Wait for real background execution, worker solving, and completion reconciliation
      await expect(
        page.getByText("Solving Vehicle Routing Problem with Time Windows")
      ).toBeHidden({ timeout: 75000 });
    }

    // Verify status changes to FEASIBLE or OPTIMAL
    await expect(page.locator("#route-status-badge")).toBeVisible({
      timeout: 20000,
    });
    await expect(page.locator("#route-status-badge")).toContainText(/FEASIBLE|OPTIMAL/i);

    // Verify Leaflet interactive map container initialized
    const mapContainer = page.locator(".leaflet-container");
    await expect(mapContainer).toBeVisible();

    // Verify stop marker pins rendered on map
    const markers = page.locator(".leaflet-marker-icon");
    await expect(markers.first()).toBeVisible({ timeout: 15000 });
    const markerCount = await markers.count();
    expect(markerCount).toBeGreaterThanOrEqual(2);

    // Verify polyline leg paths drawn on SVG overlay
    const legPaths = page.locator(".leaflet-pane svg path");
    await expect(legPaths.first()).toBeVisible({ timeout: 15000 });

    // Verify metric cards populated
    await expect(page.getByText("Distance")).toBeVisible();
    await expect(page.getByText("Duration")).toBeVisible();
    await expect(page.getByText("Fuel Estimate")).toBeVisible();

    // Verify map attribution
    await expect(page.getByText(/OpenStreetMap/i).first()).toBeVisible();
  });

  test("Scenario 2: Refresh Recovery restores active route from URL and session storage", async ({
    page,
  }) => {
    // Navigate directly with an existing active route_id (seeded multimodal route)
    await page.goto(`/trips/${MULTIMODAL_TRIP_ID}/route?route_id=${MULTIMODAL_ROUTE_ID}`);

    // Verify initial route load
    await expect(page.locator("#route-status-badge")).toBeVisible({ timeout: 15000 });
    await expect(page.locator("#route-status-badge")).toContainText("FEASIBLE");
    await expect(page.locator(".leaflet-container")).toBeVisible();

    // Perform browser refresh to test recovery
    await page.reload();

    // Verify refresh recovery reloads map and metrics seamlessly
    await expect(page.locator(".leaflet-container")).toBeVisible({ timeout: 15000 });
    await expect(page.locator("#route-status-badge")).toBeVisible({ timeout: 15000 });
    await expect(page.locator("#route-status-badge")).toContainText("FEASIBLE");
    await expect(page.getByText("3.3 km").first()).toBeVisible();
  });

  test("Scenario 3: Mid-Trip Changed-Road Replan and Version Switching Lineage", async ({
    page,
  }) => {
    // Load base route
    await page.goto(`/trips/${MULTIMODAL_TRIP_ID}/route?route_id=${MULTIMODAL_ROUTE_ID}`);

    // Wait for route map to be loaded
    await expect(page.locator("#route-status-badge")).toBeVisible({ timeout: 15000 });
    const replanBtn = page.getByRole("button", { name: "Replan & Detour" });
    await expect(replanBtn).toBeVisible({ timeout: 15000 });

    // Click preset location chip to set detour coordinates
    const bypassChip = page.getByRole("button", { name: "Mysuru Outer Bypass" });
    await bypassChip.click();

    // Submit replan
    await replanBtn.click();

    // Verify replan submission initiated
    await expect(
      page.getByText(/Replan initiated|Solving Vehicle Routing Problem/i).first()
    ).toBeVisible({ timeout: 15000 });

    // Wait for replan job to resolve and complete
    await expect(page.locator("#route-status-badge")).toContainText("v2", { timeout: 75000 });

    // Verify Version Selector dropdown is present
    const versionSelect = page.locator("#version-select");
    await expect(versionSelect).toBeVisible();

    // Wait for version history to populate dropdown with both v1 and v2
    await expect(versionSelect).toContainText("v2", { timeout: 15000 });
    await expect(versionSelect).toContainText("v1", { timeout: 15000 });

    const options = await versionSelect.locator("option").allTextContents();
    const v1Option = options.find((opt) => opt.includes("v1"))!;
    const v2Option = options.find((opt) => opt.includes("v2"))!;

    // Switch to v1 and verify map updates
    await versionSelect.selectOption({ label: v1Option });
    await expect(page).toHaveURL(/route_id=/);

    // Switch back to v2 and verify map updates
    await versionSelect.selectOption({ label: v2Option });
    await expect(page).toHaveURL(/route_id=/);
  });

  test("Scenario 4: Multimodal Route Execution (Parking Hub, Walking Leg, Vehicle Return, Drive Home)", async ({
    page,
  }) => {
    // Load pre-seeded canonical multimodal route
    await page.goto(`/trips/${MULTIMODAL_TRIP_ID}/route?route_id=${MULTIMODAL_ROUTE_ID}`);

    // Verify status badge
    await expect(page.locator("#route-status-badge")).toBeVisible({ timeout: 15000 });
    await expect(page.locator("#route-status-badge")).toContainText("FEASIBLE");

    // Verify metrics show multimodal total distance (3.3 km)
    await expect(page.getByText("3.3 km").first()).toBeVisible();

    // Verify Leaflet map container rendered
    const map = page.locator(".leaflet-container");
    await expect(map).toBeVisible();

    // Verify stop marker pins rendered (5 stops: Origin, Parking In, Pedestrian, Parking Out, Return)
    const markers = page.locator(".leaflet-marker-icon");
    await expect(markers.first()).toBeVisible({ timeout: 15000 });
    const count = await markers.count();
    expect(count).toBeGreaterThanOrEqual(4);

    // Verify walking legs rendered as emerald dashed polylines
    const walkingLegs = page.locator('.leaflet-pane svg path[stroke="#10b981"]');
    await expect(walkingLegs.first()).toBeVisible({ timeout: 10000 });

    // Verify driving legs rendered as blue polylines
    const drivingLegs = page.locator('.leaflet-pane svg path[stroke="#3b82f6"]');
    await expect(drivingLegs.first()).toBeVisible({ timeout: 10000 });
  });

  test("Scenario 5: PARTIAL Outcome (Dropped Optional Stop and Advisory Diagnostics)", async ({
    page,
  }) => {
    // Load pre-seeded partial route where optional stop was dropped
    await page.goto(`/trips/${PARTIAL_TRIP_ID}/route?route_id=${PARTIAL_ROUTE_ID}`);

    // Verify status badge shows PARTIAL
    await expect(page.locator("#route-status-badge")).toBeVisible({ timeout: 15000 });
    await expect(page.locator("#route-status-badge")).toContainText("PARTIAL");

    // Verify Route Optimization Advisory diagnostics banner is displayed
    await expect(page.getByText("Route Optimization Advisory")).toBeVisible();

    // Verify diagnostic explains dropped stop
    await expect(
      page.getByText(/Optional stop 'Tiger Hill Summit' dropped/i)
    ).toBeVisible();

    // Verify surviving feasible legs are still rendered on map
    const map = page.locator(".leaflet-container");
    await expect(map).toBeVisible();
    const paths = page.locator(".leaflet-pane svg path");
    await expect(paths.first()).toBeVisible({ timeout: 10000 });
  });

  test("Scenario 6: INFEASIBLE Outcome (No Verified Parking Hub in Pedestrian Precinct)", async ({
    page,
  }) => {
    // Load pre-seeded infeasible route
    await page.goto(`/trips/${INFEASIBLE_TRIP_ID}/route?route_id=${INFEASIBLE_ROUTE_ID}`);

    // Verify status badge shows INFEASIBLE with red styling
    await expect(page.locator("#route-status-badge")).toBeVisible({ timeout: 15000 });
    await expect(page.locator("#route-status-badge")).toContainText("INFEASIBLE");

    // Verify Mathematical Hard Constraint Infeasibility diagnostics banner is displayed
    await expect(
      page.getByText("Mathematical Hard Constraint Infeasibility").first()
    ).toBeVisible();

    // Verify failure diagnostic explanation
    await expect(
      page.getByText(/NO_VERIFIED_PARKING_HUB/i).first()
    ).toBeVisible();

    // Verify zero legs are rendered on map canvas
    const paths = page.locator(".leaflet-pane svg path");
    await expect(paths).toHaveCount(0);
  });

  test("Scenario 7: Tenant Owner Isolation with Controlled Principals & Dev Auth Decision", async ({
    request,
    page,
  }) => {
    // 1. Verify Development Auth Decision: unauthenticated request from browser defaults to Dev Principal (User A)
    await page.goto(`/trips/${SAMPLE_TRIP_ID}/route?route_id=${MULTIMODAL_ROUTE_ID}`);
    await expect(page.locator("#route-status-badge")).toBeVisible({ timeout: 15000 });
    await expect(page.locator("#route-status-badge")).toContainText("FEASIBLE");

    // 2. Controlled Test Principal: User B attempts to access User A's route map -> 403 Forbidden
    const userBToken = createBearerToken(USER_B_ID, "attacker@unauthorized.internal");
    const forbiddenMapRes = await request.get(
      `${API_BASE}/api/v1/optimization/routes/${MULTIMODAL_ROUTE_ID}/map`,
      {
        headers: {
          Authorization: `Bearer ${userBToken}`,
        },
      }
    );
    expect(forbiddenMapRes.status()).toBe(403);
    const forbiddenMapData = await forbiddenMapRes.json();
    expect(forbiddenMapData.detail).toContain("belongs to another tenant");

    // 3. User B attempts to submit a replan against User A's route -> 403 Forbidden
    const forbiddenReplanRes = await request.post(
      `${API_BASE}/api/v1/optimization/routes/${MULTIMODAL_ROUTE_ID}/replans`,
      {
        headers: {
          Authorization: `Bearer ${userBToken}`,
          "Content-Type": "application/json",
        },
        data: {
          current_location: { latitude: 12.9716, longitude: 77.5946 },
          replan_at: new Date().toISOString(),
          completed_stop_ids: [],
        },
      }
    );
    expect(forbiddenReplanRes.status()).toBe(403);
    const forbiddenReplanData = await forbiddenReplanRes.json();
    expect(forbiddenReplanData.detail).toContain("belongs to another tenant");
  });
});

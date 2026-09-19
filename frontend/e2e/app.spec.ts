import { test, expect } from "@playwright/test";

const TEST_SESSION_TOKEN =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." +
  Buffer.from(
    JSON.stringify({
      sub: "usr_swena_traveler",
      name: "Karnataka Explorer",
      email: "traveler@swena.internal",
    })
  ).toString("base64url") +
  ".test_signature";

test.describe("SWENA Travel Platform E2E Suite", () => {
  test("1. Marketing Home Page renders, displays 3D Corridor Cards, and navigation links", async ({
    page,
  }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/SWENA/);

    // Verify brand heading and core value proposition
    await expect(page.locator("text=SWENA").first()).toBeVisible();
    await expect(page.locator("text=We gave you").first()).toBeVisible();

    // Verify GSAP 3D Corridor Showcase cards
    await expect(
      page.locator("text=Signature Topographic Corridors")
    ).toBeVisible();
    await expect(
      page.locator("text=Western Ghats Monsoon Odyssey")
    ).toBeVisible();
    await expect(
      page.locator("text=Royal Rajputana Circuit")
    ).toBeVisible();
    await expect(
      page.locator("text=Konkan & Sahyadri Pass")
    ).toBeVisible();

    // Verify navigation links
    await expect(
      page.getByRole("link", { name: "Dashboard", exact: true })
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: "Benchmarks", exact: true })
    ).toBeVisible();
  });

  test("2. Unauthenticated Route Protection via Middleware & Fail-Closed OAuth Diagnostics", async ({
    page,
  }) => {
    // 1. Direct unauthenticated navigation to /dashboard must be intercepted by Next.js middleware
    await page.goto("/dashboard");
    await expect(page).toHaveURL(/.*\/login\?return_to=%2Fdashboard/);

    // 2. Verify /login page renders diagnostic setup notice
    await expect(page.locator("text=SWENA Authentication")).toBeVisible();
    await expect(page.locator("text=Sign In via SWYRA Auth")).toBeVisible();

    // 3. Unconfigured OAuth callback must fail-closed honestly (not fake tokens)
    await page.goto("/api/auth/callback?code=unconfigured_test_code");
    await expect(page).toHaveURL(/.*\/login\?error=oauth_unconfigured.*/);
    await expect(
      page.locator("text=Identity Provider Notice: oauth_unconfigured")
    ).toBeVisible();
    await expect(page.locator("text=Required Setup (.env.local)")).toBeVisible();
  });

  test("3. Authenticated Trip Planning Console & Multi-Tab Workflow", async ({
    page,
    context,
  }) => {
    // Authenticate browser context with secure session cookie
    await context.addCookies([
      {
        name: "swena_session",
        value: TEST_SESSION_TOKEN,
        domain: "localhost",
        path: "/",
      },
    ]);

    await page.goto("/dashboard");
    await expect(page).toHaveURL(/.*dashboard/);

    // Verify brief editor form
    await expect(page.locator("text=Travel Brief Specification")).toBeVisible();
    await expect(page.locator("text=Origin Hub")).toBeVisible();

    // Re-solve schedule
    const solveButton = page.getByRole("button", {
      name: /Compile & Re-solve Schedule/i,
    });
    await expect(solveButton).toBeVisible();
    await solveButton.click();

    // Tab 1: Solved Schedule
    await expect(page.locator("text=Solved Schedule")).toBeVisible();
    await expect(
      page.locator("text=Monotonically Sequenced Stops")
    ).toBeVisible();
    await expect(
      page.locator("text=Corridor Weather & Ghat Advisory")
    ).toBeVisible();

    // Tab 2: Corridor Map
    const mapTab = page.getByRole("button", { name: "Corridor Map" });
    await mapTab.click();
    await expect(
      page.locator("text=Geospatial Corridor Route Canvas")
    ).toBeVisible();
    await expect(page.locator("text=Corridor Stops Active")).toBeVisible();

    // Tab 3: Itemized Budget
    const budgetTab = page.getByRole("button", { name: "Itemized Budget" });
    await budgetTab.click();
    // Verify zero-coercion guarantee: exact copy requirement
    await expect(
      page.locator("text=Known/estimated subtotal; tolls unknown").first()
    ).toBeVisible();
    await expect(
      page.locator("text=Budget Completeness: Incomplete (Preserved)")
    ).toBeVisible();
    await expect(page.locator("text=NOT been coerced to ₹0")).toBeVisible();

    // Tab 4: Corridor Places
    const placesTab = page.getByRole("button", { name: "Corridor Places" });
    await placesTab.click();
    await expect(page.locator("text=Attribution & Ethos")).toBeVisible();
    await expect(page.locator("text=Ranganathittu Sanctuary")).toBeVisible();

    // Tab 5: Evidence Registry
    const evidenceTab = page.getByRole("button", { name: "Evidence Registry" });
    await evidenceTab.click();
    await expect(
      page.locator("text=Evidence Presentation Standard")
    ).toBeVisible();
    await expect(page.locator("text=LIVE_OFFER")).toBeVisible();
    await expect(page.locator("text=INDICATIVE_SEARCH")).toBeVisible();
    await expect(page.locator("text=EDITORIAL_DISCOVERY")).toBeVisible();
    await expect(page.getByRole("cell", { name: "UNAVAILABLE" })).toBeVisible();
  });

  test("4. Live Benchmark Runner Page (/benchmarks)", async ({ page }) => {
    await page.goto("/benchmarks");

    // Verify header and 50-scenario runner
    await expect(
      page.locator("text=50-Scenario Production Benchmark Runner")
    ).toBeVisible();
    await expect(
      page.locator("text=Engine Invariant & Real-World Evaluation")
    ).toBeVisible();

    // Verify KPI cards
    await expect(page.locator("text=50").first()).toBeVisible();
    await expect(page.locator("text=100%").first()).toBeVisible();

    // Verify category filters
    const budgetFilter = page.getByRole("button", { name: "budget" });
    await expect(budgetFilter).toBeVisible();
    await budgetFilter.click();
    await expect(page.locator("text=TRIP-037")).toBeVisible();
  });

  test("5. Public Shareable Itinerary & QR Code Modal (/trips/[id])", async ({
    page,
  }) => {
    await page.goto("/trips/karnataka-circuit-2026");

    // Verify public trip header
    await expect(
      page.locator("text=Bengaluru to Coorg Heritage & Coffee Trail")
    ).toBeVisible();
    await expect(page.locator("text=Verified Trip Plan")).toBeVisible();

    // Open QR code modal
    const qrButton = page.getByRole("button", { name: /QR Code/i });
    await expect(qrButton).toBeVisible();
    await qrButton.click();

    // Verify modal and QR Code
    await expect(page.locator("text=Open Trip on Mobile")).toBeVisible();
    await expect(page.locator("img[alt='Trip Share QR Code']")).toBeVisible();

    // Close modal
    await page.getByRole("button", { name: "Close" }).click();
    await expect(page.locator("text=Open Trip on Mobile")).not.toBeVisible();
  });

  test("6. SWYRA OAuth 2.1 BFF Session Lifecycle & Logout", async ({
    page,
    context,
  }) => {
    // 1. Initial unauthenticated state: /api/auth/me returns null user
    const meResBefore = await page.request.get("/api/auth/me");
    expect(meResBefore.status()).toBe(200);
    const meJsonBefore = await meResBefore.json();
    expect(meJsonBefore.user).toBeNull();

    // 2. Set valid session cookie
    await context.addCookies([
      {
        name: "swena_session",
        value: TEST_SESSION_TOKEN,
        domain: "localhost",
        path: "/",
      },
    ]);

    // 3. Authenticated state: /api/auth/me returns authenticated traveler
    const meResAfter = await page.request.get("/api/auth/me");
    expect(meResAfter.status()).toBe(200);
    const meJsonAfter = await meResAfter.json();
    expect(meJsonAfter.user).not.toBeNull();
    expect(meJsonAfter.user.name).toBe("Karnataka Explorer");

    // 4. Test logout
    await page.goto("/api/auth/logout");
    await expect(page).toHaveURL("http://localhost:3000/");

    // 5. Post-logout state: /api/auth/me returns null user
    const meResLoggedOut = await page.request.get("/api/auth/me");
    const meJsonLoggedOut = await meResLoggedOut.json();
    expect(meJsonLoggedOut.user).toBeNull();
  });
});

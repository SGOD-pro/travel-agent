const { chromium } = require("playwright");
const path = require("path");
const fs = require("fs");

const EVIDENCE_DIR = path.resolve(__dirname, "../../docs/marketing/evidence");
if (!fs.existsSync(EVIDENCE_DIR)) {
  fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
}

async function run() {
  console.log("=== SWENA Cinematic Marketing Site Visual Verification ===");
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });

  const results = {
    checksPassed: 0,
    checksFailed: 0,
    details: [],
  };

  function assert(condition, message) {
    if (condition) {
      results.checksPassed++;
      console.log(`[PASS] ${message}`);
      results.details.push({ status: "PASS", message });
    } else {
      results.checksFailed++;
      console.error(`[FAIL] ${message}`);
      results.details.push({ status: "FAIL", message });
    }
  }

  try {
    // -------------------------------------------------------------------------
    // 1. Desktop Viewport (1440 x 900)
    // -------------------------------------------------------------------------
    console.log("\n--- 1. Desktop Viewport (1440 x 900) ---");
    const desktopPage = await browser.newPage({
      viewport: { width: 1440, height: 900 },
      deviceScaleFactor: 2,
    });

    await desktopPage.goto("http://localhost:3000/", { waitUntil: "domcontentloaded" });
    await desktopPage.waitForTimeout(1000);

    // Verify H1 text
    const h1Text = await desktopPage.locator("h1").first().textContent();
    assert(
      h1Text.includes("Make room for") && h1Text.includes("the journey"),
      `Hero H1 text is: "${h1Text.replace(/\s+/g, ' ').trim()}"`
    );

    // Capture Hero initial state
    await desktopPage.screenshot({
      path: path.join(EVIDENCE_DIR, "desktop_01_hero_arrival.png"),
      fullPage: false,
    });
    console.log("Captured: desktop_01_hero_arrival.png");

    // Scroll through hero sticky progress
    await desktopPage.evaluate(() => window.scrollBy({ top: 800, behavior: "instant" }));
    await desktopPage.waitForTimeout(800);

    await desktopPage.screenshot({
      path: path.join(EVIDENCE_DIR, "desktop_02_hero_route_revealed.png"),
      fullPage: false,
    });
    console.log("Captured: desktop_02_hero_route_revealed.png");

    // Scroll to Scene 2: Destination Filmstrip (#explore)
    const exploreSection = desktopPage.locator("#explore");
    await exploreSection.scrollIntoViewIfNeeded();
    await desktopPage.waitForTimeout(600);

    const exploreH2 = await exploreSection.locator("h2").textContent();
    assert(
      exploreH2.includes("Find your kind of away"),
      `Destination Filmstrip H2 is: "${exploreH2.trim()}"`
    );

    await desktopPage.screenshot({
      path: path.join(EVIDENCE_DIR, "desktop_03_destination_filmstrip.png"),
      fullPage: false,
    });
    console.log("Captured: desktop_03_destination_filmstrip.png");

    // Test carousel Next button
    const nextBtn = desktopPage.locator('button[aria-label="Next corridor"]');
    if (await nextBtn.isVisible()) {
      await nextBtn.click();
      await desktopPage.waitForTimeout(600);
      await desktopPage.screenshot({
        path: path.join(EVIDENCE_DIR, "desktop_04_filmstrip_rajasthan.png"),
        fullPage: false,
      });
      console.log("Captured: desktop_04_filmstrip_rajasthan.png");
    }

    // Scroll to Scene 3: Editorial Pause
    const pauseSection = desktopPage.locator('section[aria-label="Scene 3: Editorial Pause"]');
    await pauseSection.scrollIntoViewIfNeeded();
    await desktopPage.waitForTimeout(500);

    const pauseH2 = await pauseSection.locator("h2").textContent();
    assert(
      pauseH2.includes("Leave room for the unexpected"),
      `Editorial Pause H2 is: "${pauseH2.trim()}"`
    );

    const pauseBody = await pauseSection.locator("p.font-sans").first().textContent();
    const wordCount = pauseBody.trim().split(/\s+/).length;
    assert(
      pauseBody.includes("Most travel tools force a choice between rigid tour packages") &&
      pauseBody.includes("taking in the morning light"),
      `Editorial pause body verified (${wordCount} words): "${pauseBody.trim()}"`
    );

    await desktopPage.screenshot({
      path: path.join(EVIDENCE_DIR, "desktop_05_editorial_pause.png"),
      fullPage: false,
    });
    console.log("Captured: desktop_05_editorial_pause.png");

    // Scroll to Scene 4: Trip Example (#approach)
    const tripSection = desktopPage.locator("#approach");
    await tripSection.scrollIntoViewIfNeeded();
    await desktopPage.waitForTimeout(500);

    await desktopPage.screenshot({
      path: path.join(EVIDENCE_DIR, "desktop_06_trip_example_balanced.png"),
      fullPage: false,
    });
    console.log("Captured: desktop_06_trip_example_balanced.png");

    // Test Pace Toggle: "Unhurried"
    await tripSection.getByRole("radio", { name: "Unhurried" }).click();
    await desktopPage.waitForTimeout(300);
    const unhurriedText = await tripSection.textContent();
    assert(
      unhurriedText.includes("3 Days • 2 Nights"),
      "Unhurried pace correctly updates to 3 Days • 2 Nights"
    );

    await desktopPage.screenshot({
      path: path.join(EVIDENCE_DIR, "desktop_07_trip_example_unhurried.png"),
      fullPage: false,
    });
    console.log("Captured: desktop_07_trip_example_unhurried.png");

    // Test Pace Toggle: "In-Depth"
    await tripSection.getByRole("radio", { name: "In-Depth" }).click();
    await desktopPage.waitForTimeout(300);
    const indepthText = await tripSection.textContent();
    assert(
      indepthText.includes("5 Days • 4 Nights"),
      "In-Depth pace correctly updates to 5 Days • 4 Nights"
    );

    // Verify explicit unknown item
    assert(
      indepthText.includes("Ghat Corridor Entry Permit") &&
      indepthText.includes("Unknown fee"),
      "Budget ledger explicitly flags Ghat Corridor Entry Permit as unknown fee (no zero coercion)"
    );

    await desktopPage.screenshot({
      path: path.join(EVIDENCE_DIR, "desktop_08_trip_example_indepth.png"),
      fullPage: false,
    });
    console.log("Captured: desktop_08_trip_example_indepth.png");

    // Scroll to Scene 5: Practical FAQ
    const faqSection = desktopPage.locator('section[aria-label="Scene 5: Practical Reassurance FAQ"]');
    await faqSection.scrollIntoViewIfNeeded();
    await desktopPage.waitForTimeout(400);

    // Open first FAQ item
    const firstFaqTrigger = faqSection.locator("button").first();
    await firstFaqTrigger.click();
    await desktopPage.waitForTimeout(400);

    await desktopPage.screenshot({
      path: path.join(EVIDENCE_DIR, "desktop_09_practical_faq.png"),
      fullPage: false,
    });
    console.log("Captured: desktop_09_practical_faq.png");

    // Scroll to Scene 6: Closing Scene & Footer
    const closingSection = desktopPage.locator('section[aria-label="Scene 6: Closing Invitation"]');
    await closingSection.scrollIntoViewIfNeeded();
    await desktopPage.waitForTimeout(500);

    const closingH2 = await closingSection.locator("h2").textContent();
    assert(
      closingH2.includes("Where will you go next?"),
      `Closing Scene H2 is: "${closingH2.trim()}"`
    );

    await desktopPage.screenshot({
      path: path.join(EVIDENCE_DIR, "desktop_10_closing_scene.png"),
      fullPage: false,
    });
    console.log("Captured: desktop_10_closing_scene.png");

    await desktopPage.close();

    // -------------------------------------------------------------------------
    // 2. Mobile Viewport (390 x 844)
    // -------------------------------------------------------------------------
    console.log("\n--- 2. Mobile Viewport (390 x 844) ---");
    const mobilePage = await browser.newPage({
      viewport: { width: 390, height: 844 },
      deviceScaleFactor: 2,
    });

    await mobilePage.goto("http://localhost:3000/", { waitUntil: "domcontentloaded" });
    await mobilePage.waitForTimeout(800);

    // Check horizontal overflow
    const hasHorizontalOverflow = await mobilePage.evaluate(() => {
      return document.documentElement.scrollWidth > window.innerWidth;
    });
    assert(!hasHorizontalOverflow, "Mobile viewport has ZERO horizontal scrollbar overflow");

    await mobilePage.screenshot({
      path: path.join(EVIDENCE_DIR, "mobile_01_hero.png"),
      fullPage: false,
    });
    console.log("Captured: mobile_01_hero.png");

    // Scroll to filmstrip
    await mobilePage.locator("#explore").scrollIntoViewIfNeeded();
    await mobilePage.waitForTimeout(500);
    await mobilePage.screenshot({
      path: path.join(EVIDENCE_DIR, "mobile_02_filmstrip.png"),
      fullPage: false,
    });
    console.log("Captured: mobile_02_filmstrip.png");

    // Scroll to trip example
    await mobilePage.locator("#approach").scrollIntoViewIfNeeded();
    await mobilePage.waitForTimeout(500);
    await mobilePage.screenshot({
      path: path.join(EVIDENCE_DIR, "mobile_03_trip_example.png"),
      fullPage: false,
    });
    console.log("Captured: mobile_03_trip_example.png");

    await mobilePage.close();

    // -------------------------------------------------------------------------
    // 3. Corridor Handoff into Dashboard
    // -------------------------------------------------------------------------
    console.log("\n--- 3. Corridor Handoff into Dashboard ---");
    const dashContext = await browser.newContext({
      viewport: { width: 1440, height: 900 },
    });
    const testPayload = Buffer.from(
      JSON.stringify({ sub: "usr_test", name: "Devyani", email: "devyani@swena.travel" })
    ).toString("base64url");
    await dashContext.addCookies([
      { name: "swena_session", value: `hdr.${testPayload}.sig`, domain: "localhost", path: "/" },
    ]);
    const dashPage = await dashContext.newPage();

    // Test Rajasthan corridor
    await dashPage.goto("http://localhost:3000/dashboard?corridor=rajasthan", { waitUntil: "domcontentloaded" });
    await dashPage.waitForTimeout(1000);

    const rajasthanContent = await dashPage.textContent("body");
    assert(
      rajasthanContent.includes("Delhi") && rajasthanContent.includes("Jaipur"),
      "Navigating with ?corridor=rajasthan pre-populates Delhi origin and Jaipur stop"
    );

    await dashPage.screenshot({
      path: path.join(EVIDENCE_DIR, "corridor_handoff_rajasthan.png"),
      fullPage: false,
    });
    console.log("Captured: corridor_handoff_rajasthan.png");

    // Test Konkan corridor
    await dashPage.goto("http://localhost:3000/dashboard?corridor=konkan", { waitUntil: "domcontentloaded" });
    await dashPage.waitForTimeout(1000);

    const konkanContent = await dashPage.textContent("body");
    assert(
      konkanContent.includes("Mumbai") && konkanContent.includes("Goa"),
      "Navigating with ?corridor=konkan pre-populates Mumbai origin and Goa stop"
    );

    await dashPage.screenshot({
      path: path.join(EVIDENCE_DIR, "corridor_handoff_konkan.png"),
      fullPage: false,
    });
    console.log("Captured: corridor_handoff_konkan.png");

    await dashPage.close();

    // -------------------------------------------------------------------------
    // 4. Contact Form Submission
    // -------------------------------------------------------------------------
    console.log("\n--- 4. Contact Form Live Submission ---");
    const contactPage = await browser.newPage({
      viewport: { width: 1440, height: 900 },
    });

    await contactPage.goto("http://localhost:3000/contact", { waitUntil: "domcontentloaded" });
    await contactPage.waitForTimeout(800);

    // Fill contact form
    await contactPage.fill('input#contact-name', 'Devyani Sen');
    await contactPage.fill('input#contact-email', 'devyani.sen@example.com');
    await contactPage.selectOption('select#contact-topic', 'Bespoke Corridor Curation');
    await contactPage.selectOption('select#contact-corridor', 'Western Ghats');
    await contactPage.fill('textarea#contact-message', 'Inquiring about 5-day road itinerary through Coorg coffee estates and Wayanad pass.');

    // Submit form
    await contactPage.click('button[type="submit"]');

    // Wait for submission card
    const successCard = contactPage.locator("text=Inquiry Authenticated & Logged");
    await successCard.waitFor({ timeout: 6000 });

    const contactBody = await contactPage.textContent("body");
    assert(
      contactBody.includes("Reference Ticket:"),
      "Contact form displays verified server ticket ID"
    );
    assert(
      !contactBody.includes("inq_"),
      "Contact form strictly avoids fake inq_ simulated IDs"
    );

    await contactPage.screenshot({
      path: path.join(EVIDENCE_DIR, "contact_form_submitted.png"),
      fullPage: false,
    });
    console.log("Captured: contact_form_submitted.png");

    await contactPage.close();

    // -------------------------------------------------------------------------
    // 5. About Page
    // -------------------------------------------------------------------------
    console.log("\n--- 5. About Page ---");
    const aboutPage = await browser.newPage({
      viewport: { width: 1440, height: 900 },
    });
    await aboutPage.goto("http://localhost:3000/about", { waitUntil: "domcontentloaded" });
    await aboutPage.waitForTimeout(600);

    const aboutH1 = await aboutPage.locator("h1").textContent();
    assert(
      aboutH1.includes("We gave you") && aboutH1.includes("memory"),
      `About page H1: "${aboutH1.replace(/\s+/g, ' ').trim()}"`
    );

    await aboutPage.screenshot({
      path: path.join(EVIDENCE_DIR, "about_page.png"),
      fullPage: false,
    });
    console.log("Captured: about_page.png");

    await aboutPage.close();

  } catch (err) {
    console.error("Test execution encountered an error:", err);
    results.checksFailed++;
    results.details.push({ status: "ERROR", message: err.message });
  } finally {
    await browser.close();
  }

  console.log("\n=================================================");
  console.log(`VERIFICATION SUMMARY: ${results.checksPassed} PASSED, ${results.checksFailed} FAILED`);
  console.log("=================================================");

  if (results.checksFailed > 0) {
    process.exit(1);
  }
}

run();

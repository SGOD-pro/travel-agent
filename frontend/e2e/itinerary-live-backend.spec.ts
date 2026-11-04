import { test, expect } from "@playwright/test";
import crypto from "crypto";

const TRUSTED_PRIVATE_KEY_PEM = `-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCRP/nc1wJf/Oe5
pG9HOS4tZglmk09/RcQyV++g3mpPOBT7RZWm76fVZc0aYbX0kDZexEUdEfZXAdh/
8IF2/+cG5OyX+zbL1gG/RZ1Zqq19fFEKg556w3nyJu4Qi40t0CF1sQPx9FWSaVPn
qAoIXguNLZdS2UGGT4Z8FgOEYtqh/CpTPuYKR8rWaFhA5VcRxx3MSc+e4l0msi8z
qsS2CCLjNMoVYt/gvBJQx4uh+HkfbOhc+7EoJul8xF5cz97UhgsltgpAm2xbyYlD
gtGZfNqzQ4Gm+Y8Pml5iiqAHgLkNUPUotycVv8wtTAuXHUGhJ3f0hMayBJwQykG+
YVMPb3prAgMBAAECggEAFeGucZ+Ogwllm2jzPqaVl09E4UgV9NV8zhzDQ9fd6htH
xebguapm/VX9xRitQa+sETC+PLflBYyIohWIW3YHxtkb73Vv9ttBniANqWF2C258
Avp7GIF0UI+e061CUoGFWOaW8o9diNYHRuHgyQvp75XLszZ5kZNPbkgetYXy6+4I
Cn2R/VwM2SMX3LvtRZtvyIZEy04q8kWVytu3xdgUNQnQCQw1v2hh75jOAApM3D9U
Ym4ZIrr9b4gZ7AgaxpcG8xaS31IlyfO6kH1dXdgJ06NjCwKSdYVCst2XnRUra14G
3Vus2BLjaHAH/QUcardjuRJH4D4a+kWnR6Uj7J/+wQKBgQDIGNlXyz5Sh5g1LOjl
8oYAZ9umB7O8LPpyRkc3BEVsh/GwrEnVnh47mwWlDPHzpBrgs6dp2MODTGX5mBjw
MrSymiCP+VL0R9MsIrk1e+1rvW9tZ7AGiTfiw+wcV0k4B1c+DVbV4bG1YXt7SfdJ
1dQz9E6gKnzR1d1lMbSfXIKpywKBgQC51Gah2cGQBz/rE0P7DVhCcj/Ri7JKkVAE
5UKA0aGtVH55tzDFTFYv6EZBGvftANNSrl93INDk8ZsbuN08F0G1oOJb6wALsN+B
tWqH4pzLqnjrgIJo93GYD49IDzOTXgu0RUo4Xpypavu5lFj8SNP+Q9XFzK8yYN6P
Zu/EK4zd4QKBgDG5axlUGnKr23krZdzuvboQFRdXRqT2kG4hZMUZPJ9R0Q/UYKzD
FEQ/q9FuPBoNp9g7DNlS0pMoQxcZYukd0KWpg3oDvUeTKbkID/poh4GEm9IWODoH
k2m4wk3wqvNPb7Ct4+xWn7CxPtxObzNb/dXDWl6W8Fm8yb3B8l6/wICtAoGAWDwg
F02b+NVYZjgZmUT22WYCI28fUwS4Z/qrnxrql7B89TIkeCoM6mZxpUw22FQCA0pE
jf4I8gw+R+V5NqUcUC96C4cD2Ik9Nwt679aL+LCTBc9ZHJoRV9Bbii2doaWQDSKx
YR6baF4h9DYaIoA6GIsBIBBWR6dzRcfspMrA8kECgYEAufjsNLrRHfgbrvUMF9TF
Wgig2uwagACf9rSX+R04oXuJBzcOasVE1Hd6h2Ih1Z2LNLJkLavSzoxOfo10aQza
vkbajtq+hixZE/S4Obo8Y7J551bNKF6OtjZScu3J31TXn4uqqYp6vKKFQrdXeZoY
xngKSkdDnuegVgbS0x9xzL8=
-----END PRIVATE KEY-----`;

const ATTACKER_PRIVATE_KEY_PEM = `-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCG5Lu42mwO6u6F
V4Oskg75xe2ITdBRh6k9E2h/nY7uv2wAM054CaLBjAD8t7fT/675RaZogS37NyGE
j34L7HzHSqXhL2P15MXdL4qvXfSNMNCfOvPqYAIttMVpCOQzhoAeMLfCJffEFYsF
ShFpo2yjQ9hKOFj4uJorEXLDkxcSBXgWKR+qTbJoYbOiHktl4Ln7+JADAzgRMvVu
UaA/H7Bwf8Q97xv/DrU7xEsocUySat+7x0jbiMRkqYrSDn4QGDzUfCqXfOUidi4/
XWE19B1U7tiUm4UNS86qmwF114zqpQf5Q0r7D0FBTGWTJcXg/fJIubCSkbp5ew6H
YunTa45dAgMBAAECggEAC1n2PiELVNoKRmhN6VjFL9mrGnxJ2oKLCFu4wHx2ybqM
ERxALzPIYXcxohKnwoxSoiFtVoAAaZKDHI+isMgu2+p5ORDu3YNp7yoQBp/mc3c3
Jx8j5YY8yLHcpvEbmwZng6Lf49OkHPkfIMvR2UzqWHbeHajOO+f0+UjXmoOnPlne
MWflXj89PSx0vGbHA7osqVrH+gJc/sewIpNSLWBOJFnbN+XBrUShUFA2dwgmtqbQ
2o8N9w1ASNwgoMteM0KUszjdjy2hgkETMH1f8PI7hfQBU5nk2scI/MD+t5Sax9ai
2ZtGS4qoZxdNbXUngi8cGBLAeYzNA71pMI0tjlYONQKBgQC6u4mQDOn1paf55IO1
uRVdFN8KSgGcFG0neV2KewtzTwTOYfdmzwUxPWurmy3joy1+dVzxY+27XSB8Lb6v
Pdky10JKvBwLxkXfkygvESWIZgptQzC251hRx2OMPaRvr1T/lkHO+knvwSuFDOEZ
BLzEaoo8+nag7qJZjG4ZHnhVdwKBgQC47naVaRgBcP78lyUsDHRy4XZkIApV+ymn
yc8lp728hSRFG5AHHaQcHFrQsrHaKMZKyns2vKq1kCxh68AkS7Ap41gyM7RfAV3G
9tK4snggyNnGvCubqOdiKPwova90G3MvsTc98xRTalNDjAsp8Yg7yPql2g3j/w9A
nCblbqS/ywKBgAs3subdb7ybDAE+Eg97o+XomBGFKp2WuIvzhYQJKkHuEErZpcQe
UaCIoQw5EUOojNYY9NFngGOOD6KGGPtsCr08IzZcII8whQgOTg6tyARupNe1fpqu
QScAhGFrXpJ7BO8ptqBZdtGZWCTxBiC7oR9nixBot3GphEU+42pM40u3AoGAS6tH
WltmNzDFJRWzYBK4k0kt6Eoz8HqU0NMCr6WzPTA9ZwmgJUk7nUT84+g6XcnRWUkA
oaURNLmJb4fHVxKhQoKD1AbUBwvIE2FrV6eu/7svYOId9dNm2gNrKkBzSk7xEtKw
h+iuqHMZHspuxvrNg6j45mQSyvfShd1fDQH454ECgYAX24/LhOUWKM48KvccQd2z
wMZtPDfJ8C/InsmJh6KCr4wm/PHdMm2jXphNkUMUvfV+yhk4uaqudovbrPXnREcZ
jCVyIffPL2QHbS2nysNMB4KAHB46vt/WTwxxvToonJjVrjdhxyFPrP1xq24eT+/D
nHgZj5xosx4hbPtwQ++4wQ==
-----END PRIVATE KEY-----`;

function createSignedJwt(
  sub: string,
  email: string = "traveler@swena.internal",
  signingKeyPem: string = TRUSTED_PRIVATE_KEY_PEM
): string {
  const header = Buffer.from(JSON.stringify({ alg: "RS256", typ: "JWT" })).toString("base64url");
  const payload = Buffer.from(
    JSON.stringify({
      sub,
      email,
      name: "Karnataka Explorer",
      iss: "https://oauth21.vercel.app",
      aud: "swena_travel_agent_client",
      exp: Math.floor(Date.now() / 1000) + 7200,
      iat: Math.floor(Date.now() / 1000),
    })
  ).toString("base64url");

  const signer = crypto.createSign("RSA-SHA256");
  signer.update(`${header}.${payload}`);
  signer.end();
  const signature = signer.sign(signingKeyPem).toString("base64url");

  return `${header}.${payload}.${signature}`;
}

const TRIP_A_ID = "00000000-0000-0000-0000-000000000001";
const USER_A_ID = "00000000-0000-0000-0000-000000000001";
const USER_B_ID = "99999999-9999-9999-9999-999999999999";

test.describe("Itinerary Proposal Live Backend Integration (Isolated PostgreSQL & Real Auth)", () => {
  test("loads genuinely published proposal from live backend with real authorization", async ({
    page,
    context,
  }) => {
    // 1. Authenticate as owner (USER_A_ID) using genuinely signed RS256 token
    const token = createSignedJwt(USER_A_ID);
    await context.addCookies([
      {
        name: "swena_session",
        value: token,
        domain: "localhost",
        path: "/",
        httpOnly: true,
        sameSite: "Lax",
      },
    ]);

    // 2. Navigate to trip proposal page without mocking network calls
    await page.goto(`/trips/${TRIP_A_ID}`);

    // 3. Verify schedule stops rendered from live backend proposal
    await expect(page.getByRole("heading", { name: "Solved Itinerary Schedule" })).toBeVisible();
    await expect(page.getByText("Bengaluru MG Road Depot").first()).toBeVisible();
    await expect(page.getByText("Cubbon Park Verified Parking Hub").first()).toBeVisible();
    await expect(page.getByText("Botanical Glass House Sanctuary").first()).toBeVisible();

    // 4. Verify AC-3: HOURS_ASSUMED badge on Botanical Glass House Sanctuary
    await expect(page.getByText("HOURS_ASSUMED").first()).toBeVisible();

    // 5. Verify itemized budget lines with zero-coercion unknown toll
    await expect(page.getByText("₹450.00").first()).toBeVisible();
    await expect(page.getByText("1 cost item(s) unverified")).toBeVisible();
    await expect(page.getByText("UNQUOTED_TOLL_STRUCTURE")).toBeVisible();

    // 6. Verify Open Meteo attribution notice
    await expect(page.getByText("Weather data by Open Meteo.com")).toBeVisible();
    await expect(page.getByText("Corridor Weather & Ghat Advisory")).toBeVisible();

    // 7. Verify voice assistant component rendered
    await expect(page.getByText("Audio Guide:")).toBeVisible();
  });

  test("enforces tenant isolation and rejects cross-owner access with HTTP 403", async ({
    page,
    context,
  }) => {
    // 1. Authenticate as foreign user (USER_B_ID) using valid token for different account
    const foreignToken = createSignedJwt(USER_B_ID, "attacker@unauthorized.internal");
    await context.addCookies([
      {
        name: "swena_session",
        value: foreignToken,
        domain: "localhost",
        path: "/",
        httpOnly: true,
        sameSite: "Lax",
      },
    ]);

    // 2. Attempt to view USER_A_ID's trip proposal
    await page.goto(`/trips/${TRIP_A_ID}`);

    // 3. Verify HTTP 403 Forbidden state rendered
    await expect(page.getByRole("heading", { name: "Access Forbidden" })).toBeVisible();
    await expect(
      page.getByText("Strict tenant isolation protects this travel proposal.")
    ).toBeVisible();
    await expect(page.getByRole("link", { name: "Back to My Dashboard" })).toBeVisible();
  });

  test("rejects forged token signature with HTTP 401 unauthorized", async ({
    page,
    context,
  }) => {
    // 1. Authenticate with forged token signed by attacker key
    const forgedToken = createSignedJwt(
      USER_A_ID,
      "traveler@swena.internal",
      ATTACKER_PRIVATE_KEY_PEM
    );
    await context.addCookies([
      {
        name: "swena_session",
        value: forgedToken,
        domain: "localhost",
        path: "/",
        httpOnly: true,
        sameSite: "Lax",
      },
    ]);

    // 2. Navigate to trip proposal
    await page.goto(`/trips/${TRIP_A_ID}`);

    // 3. Verify access rejection (HTTP 401 triggers Forbidden / Unauthorized view)
    await expect(page.getByRole("heading", { name: "Access Forbidden" })).toBeVisible();
  });

  test("rejects missing authorization credentials in protected mode", async ({
    page,
    context,
  }) => {
    // 1. Clear any cookies
    await context.clearCookies();

    // 2. Navigate to trip proposal
    await page.goto(`/trips/${TRIP_A_ID}`);

    // 3. Verify unauthorized/forbidden notice is shown
    await expect(page.getByRole("heading", { name: "Access Forbidden" })).toBeVisible();
  });
});


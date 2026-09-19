import { NextResponse } from "next/server";
import crypto from "crypto";

export async function GET(request: Request) {
  const authIssuer = process.env.AUTH_ISSUER || "https://oauth21.vercel.app";
  const clientId = process.env.CLIENT_ID || "swena_travel_agent_client";
  const origin = new URL(request.url).origin;
  const redirectUri = process.env.REDIRECT_URI || `${origin}/api/auth/callback`;

  // Generate PKCE code_verifier (43-128 chars base64url)
  const codeVerifier = crypto.randomBytes(32).toString("base64url");
  // Calculate code_challenge = base64url(sha256(codeVerifier))
  const codeChallenge = crypto
    .createHash("sha256")
    .update(codeVerifier)
    .digest("base64url");

  // Generate random state for CSRF mitigation
  const state = crypto.randomBytes(16).toString("hex");

  // Build OAuth 2.1 authorization URL (per SGOD-pro/OAuth2.1 RFC 8252 spec)
  const authUrl = new URL(`${authIssuer}/auth`);
  authUrl.searchParams.set("client_id", clientId);
  authUrl.searchParams.set("redirect_uri", redirectUri);
  authUrl.searchParams.set("response_type", "code");
  authUrl.searchParams.set("code_challenge", codeChallenge);
  authUrl.searchParams.set("code_challenge_method", "S256");
  authUrl.searchParams.set("scope", "openid profile email");
  authUrl.searchParams.set("state", state);

  const response = NextResponse.redirect(authUrl.toString());

  // Store PKCE verifier and state in secure cookies
  response.cookies.set("oauth_pkce_verifier", codeVerifier, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 600, // 10 minutes
  });

  response.cookies.set("oauth_state", state, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 600,
  });

  return response;
}

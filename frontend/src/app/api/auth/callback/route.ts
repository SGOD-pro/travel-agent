import { NextResponse } from "next/server";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const code = searchParams.get("code");
  const state = searchParams.get("state");
  const error = searchParams.get("error");
  const errorDescription = searchParams.get("error_description");
  const origin = new URL(request.url).origin;

  // Handle upstream error from authorization server
  if (error) {
    const message = errorDescription || error;
    return NextResponse.redirect(
      new URL(`/login?error=${encodeURIComponent(message)}`, request.url)
    );
  }

  if (!code) {
    return NextResponse.redirect(
      new URL("/login?error=missing_authorization_code", request.url)
    );
  }

  // Retrieve PKCE verifier from cookies
  const cookieHeader = request.headers.get("cookie") || "";
  const getCookie = (name: string) => {
    const match = cookieHeader.match(new RegExp(`(^|;\\s*)(${name})=([^;]*)`));
    return match ? decodeURIComponent(match[3]) : null;
  };

  const codeVerifier = getCookie("oauth_pkce_verifier");
  const storedState = getCookie("oauth_state");

  // Verify CSRF state
  if (storedState && state !== storedState) {
    return NextResponse.redirect(
      new URL("/login?error=state_mismatch_csrf_detected", request.url)
    );
  }

  const authIssuer = process.env.AUTH_ISSUER;
  const clientId = process.env.CLIENT_ID;
  const clientSecret = process.env.CLIENT_SECRET;
  const redirectUri = process.env.REDIRECT_URI || `${origin}/api/auth/callback`;

  // Fail-fast if OAuth credentials are not configured in environment
  if (!authIssuer || !clientId) {
    return NextResponse.redirect(
      new URL(
        "/login?error=oauth_unconfigured&message=AUTH_ISSUER+or+CLIENT_ID+not+set+in+env",
        request.url
      )
    );
  }

  try {
    const headers: Record<string, string> = {
      "Content-Type": "application/x-www-form-urlencoded",
    };

    if (clientSecret) {
      headers["Authorization"] = `Basic ${Buffer.from(`${clientId}:${clientSecret}`).toString("base64")}`;
    }

    const bodyParams = new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri,
      client_id: clientId,
    });

    if (codeVerifier) {
      bodyParams.set("code_verifier", codeVerifier);
    }

    const tokenRes = await fetch(`${authIssuer}/api/auth/oauth2/token`, {
      method: "POST",
      headers,
      body: bodyParams,
    });

    if (!tokenRes.ok) {
      const errorText = await tokenRes.text();
      let errorMsg = `Token exchange failed with status ${tokenRes.status}`;
      try {
        const errorJson = JSON.parse(errorText);
        errorMsg = errorJson.error_description || errorJson.error || errorMsg;
      } catch {
        // use raw text
      }
      return NextResponse.redirect(
        new URL(`/login?error=token_exchange_failed&details=${encodeURIComponent(errorMsg)}`, request.url)
      );
    }

    const tokens = await tokenRes.json();
    const accessToken = tokens.access_token;

    if (!accessToken) {
      return NextResponse.redirect(
        new URL("/login?error=no_access_token_returned", request.url)
      );
    }

    const response = NextResponse.redirect(new URL("/dashboard", request.url));

    // Set secure HttpOnly session cookie
    response.cookies.set("swena_session", accessToken, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: 86400 * 7, // 7 days
    });

    // Clean up temporary OAuth cookies
    response.cookies.set("oauth_pkce_verifier", "", { path: "/", maxAge: 0 });
    response.cookies.set("oauth_state", "", { path: "/", maxAge: 0 });

    return response;
  } catch (err: unknown) {
    const errorMsg = err instanceof Error ? err.message : String(err);
    return NextResponse.redirect(
      new URL(
        `/login?error=idp_connection_error&details=${encodeURIComponent(errorMsg)}`,
        request.url
      )
    );
  }
}

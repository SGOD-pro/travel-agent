import { NextResponse } from "next/server";

export async function GET(request: Request) {
  const cookieHeader = request.headers.get("cookie") || "";
  const match = cookieHeader.match(/(^|;\s*)swena_session=([^;]*)/);
  const sessionToken = match ? decodeURIComponent(match[2]) : null;

  if (!sessionToken || sessionToken.trim() === "") {
    return NextResponse.json({ user: null, token: null });
  }

  try {
    let payload: Record<string, unknown> = {};

    if (sessionToken.includes(".")) {
      const parts = sessionToken.split(".");
      if (parts.length >= 2) {
        const decodedPayload = Buffer.from(parts[1], "base64url").toString("utf-8");
        payload = JSON.parse(decodedPayload);
      }
    } else {
      const decodedPayload = Buffer.from(sessionToken, "base64url").toString("utf-8");
      payload = JSON.parse(decodedPayload);
    }

    const exp = payload.exp as number | undefined;
    if (exp && typeof exp === "number" && exp * 1000 < Date.now()) {
      return NextResponse.json({ user: null, token: null });
    }

    const sub = (payload.sub as string) || "";
    if (!sub) {
      return NextResponse.json({ user: null, token: null });
    }

    return NextResponse.json({
      user: {
        id: sub,
        name: (payload.name as string) || "Karnataka Explorer",
        email: (payload.email as string) || "traveler@swena.internal",
      },
      token: sessionToken,
    });
  } catch {
    return NextResponse.json({ user: null, token: null });
  }
}

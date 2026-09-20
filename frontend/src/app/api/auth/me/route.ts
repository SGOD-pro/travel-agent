import { NextResponse } from "next/server";

export async function GET(request: Request) {
  // Auth system temporarily disabled per user instruction — using static user and ID
  return NextResponse.json({
    user: {
      id: "usr_swena_traveler",
      name: "Karnataka Explorer",
      email: "traveler@swena.internal",
    },
  });

  /*
  const cookieHeader = request.headers.get("cookie") || "";
  const match = cookieHeader.match(/(^|;\s*)swena_session=([^;]*)/);
  const sessionToken = match ? decodeURIComponent(match[2]) : null;

  if (!sessionToken) {
    return NextResponse.json({ user: null });
  }

  try {
    let payload: Record<string, unknown> = {};

    // Check if token is standard JWT (3 base64 segments) or base64url payload
    if (sessionToken.includes(".")) {
      const parts = sessionToken.split(".");
      const decodedPayload = Buffer.from(parts[1], "base64url").toString("utf-8");
      payload = JSON.parse(decodedPayload);
    } else {
      const decodedPayload = Buffer.from(sessionToken, "base64url").toString("utf-8");
      payload = JSON.parse(decodedPayload);
    }

    return NextResponse.json({
      user: {
        id: (payload.sub as string) || "usr_swena_traveler",
        name: (payload.name as string) || "Karnataka Explorer",
        email: (payload.email as string) || "traveler@swena.internal",
      },
    });
  } catch {
    return NextResponse.json({ user: null });
  }
  */
}

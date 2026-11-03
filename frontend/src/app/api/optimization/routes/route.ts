import { NextResponse } from "next/server";

import { proxyAuthenticatedBackendRequest } from "@/lib/backend-api";

export async function POST(request: Request) {
  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "Invalid JSON request body." }, { status: 400 });
  }

  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return NextResponse.json({ detail: "Invalid request body." }, { status: 400 });
  }
  return proxyAuthenticatedBackendRequest(request, "/api/v1/optimization/routes", body);
}

import { NextResponse } from "next/server";

import { proxyAuthenticatedBackendRequest } from "@/lib/backend-api";

export async function POST(request: Request) {
  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "Invalid JSON request body." }, { status: 400 });
  }

  if (
    !body ||
    typeof body !== "object" ||
    Array.isArray(body) ||
    !body.brief ||
    typeof body.brief !== "object"
  ) {
    return NextResponse.json({ detail: "A trip brief is required." }, { status: 422 });
  }

  return proxyAuthenticatedBackendRequest(request, "/api/v1/trips", {
    brief: body.brief,
  });
}

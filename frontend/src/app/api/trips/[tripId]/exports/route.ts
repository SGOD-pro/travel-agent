import { NextResponse } from "next/server";

import { proxyAuthenticatedBackendRequest } from "@/lib/backend-api";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export async function POST(
  request: Request,
  { params }: { params: Promise<{ tripId: string }> }
) {
  const { tripId } = await params;
  if (!UUID_PATTERN.test(tripId)) {
    return NextResponse.json({ detail: "Invalid trip ID." }, { status: 400 });
  }

  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "Invalid JSON request body." }, { status: 400 });
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return NextResponse.json({ detail: "Invalid request body." }, { status: 400 });
  }
  return proxyAuthenticatedBackendRequest(
    request,
    `/api/v1/trips/${tripId}/exports`,
    body
  );
}

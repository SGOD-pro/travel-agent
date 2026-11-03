import { NextResponse } from "next/server";

import { proxyAuthenticatedBackendRequest } from "@/lib/backend-api";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export async function GET(
  request: Request,
  { params }: { params: Promise<{ artifactId: string }> }
) {
  const { artifactId } = await params;
  if (!UUID_PATTERN.test(artifactId)) {
    return NextResponse.json({ detail: "Invalid artifact ID." }, { status: 400 });
  }
  return proxyAuthenticatedBackendRequest(
    request,
    `/api/v1/artifacts/${artifactId}/download`
  );
}

import { NextResponse } from "next/server";

import { proxyAuthenticatedBackendRequest } from "@/lib/backend-api";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export async function GET(
  request: Request,
  { params }: { params: Promise<{ jobId: string }> }
) {
  const { jobId } = await params;
  if (!UUID_PATTERN.test(jobId)) {
    return NextResponse.json({ detail: "Invalid job ID." }, { status: 400 });
  }

  const url = new URL(request.url);
  const query = new URLSearchParams();
  for (const name of ["cursor", "limit"]) {
    const value = url.searchParams.get(name);
    if (value !== null) query.set(name, value);
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return proxyAuthenticatedBackendRequest(
    request,
    `/api/v1/jobs/${jobId}/events${suffix}`
  );
}

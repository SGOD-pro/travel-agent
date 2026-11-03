import { NextResponse } from "next/server";

import { backendBaseUrl } from "@/lib/backend-api";

export async function GET() {
  try {
    const response = await fetch(`${backendBaseUrl()}/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
    return NextResponse.json(
      { status: response.ok ? "connected" : "offline" },
      { status: response.ok ? 200 : 503 }
    );
  } catch {
    return NextResponse.json({ status: "offline" }, { status: 503 });
  }
}

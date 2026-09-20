import { NextResponse } from "next/server";

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const backendBase =
      process.env.BACKEND_INTERNAL_URL ||
      process.env.NEXT_PUBLIC_API_BASE_URL ||
      "http://localhost:8000";

    const res = await fetch(`${backendBase}/api/v1/support/inquiries`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (err: unknown) {
    const errorMessage = err instanceof Error ? err.message : "Unknown network error";
    return NextResponse.json(
      { error: "Concierge backend unreachable", detail: errorMessage },
      { status: 503 }
    );
  }
}

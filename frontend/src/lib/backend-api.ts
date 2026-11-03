import { cookies } from "next/headers";
import { NextResponse } from "next/server";

export async function proxyAuthenticatedBackendRequest(
  request: Request,
  path: string,
  body?: Record<string, unknown>
): Promise<NextResponse> {
  const token = (await cookies()).get("swena_session")?.value;
  const localAuthBypass =
    process.env.NODE_ENV === "development" && process.env.LOCAL_AUTH_BYPASS === "true";
  if (!token && !localAuthBypass) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const backendBase = backendBaseUrl();
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
  };
  if (!token && localAuthBypass) delete headers.Authorization;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const idempotencyKey = request.headers.get("Idempotency-Key");
  if (idempotencyKey) {
    headers["Idempotency-Key"] = idempotencyKey;
  }

  try {
    const response = await fetch(`${backendBase}${path}`, {
      method: request.method,
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      cache: "no-store",
    });

    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      const responseText = await response.text();
      try {
        return NextResponse.json(JSON.parse(responseText), {
          status: response.status,
        });
      } catch {
        return NextResponse.json(
          { detail: "Backend returned an invalid JSON response." },
          { status: 502 }
        );
      }
    }

    const forwardedHeaders = new Headers();
    for (const headerName of ["content-type", "content-disposition", "cache-control"]) {
      const value = response.headers.get(headerName);
      if (value) forwardedHeaders.set(headerName, value);
    }
    return new NextResponse(response.body, {
      status: response.status,
      headers: forwardedHeaders,
    });
  } catch {
    return NextResponse.json(
      { detail: "Backend is temporarily unavailable." },
      { status: 503 }
    );
  }
}

export function backendBaseUrl(): string {
  return (
    process.env.BACKEND_INTERNAL_URL ||
    process.env.NEXT_PUBLIC_API_BASE_URL ||
    "http://localhost:8000"
  );
}

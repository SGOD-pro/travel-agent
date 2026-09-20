import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;

  // Temporarily disabled auth protection for local development (using static user and ID)
  // const isProtectedRoute = pathname.startsWith("/dashboard");
  // if (isProtectedRoute) {
  //   const sessionCookie = request.cookies.get("swena_session");
  //   const sessionToken = sessionCookie?.value;
  //   if (!sessionToken || sessionToken.trim() === "") {
  //     const returnTo = `${pathname}${search}`;
  //     const loginUrl = new URL("/login", request.url);
  //     loginUrl.searchParams.set("return_to", returnTo);
  //     return NextResponse.redirect(loginUrl);
  //   }
  // }

  return NextResponse.next();
}

export const config = {
  matcher: [
    "/dashboard",
    "/dashboard/:path*",
  ],
};

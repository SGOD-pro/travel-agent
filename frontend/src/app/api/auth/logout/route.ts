import { NextResponse } from "next/server";

export async function POST(request: Request) {
  const origin = new URL(request.url).origin;
  const response = NextResponse.json({ success: true });
  response.cookies.set("swena_session", "", { path: "/", maxAge: 0 });
  return response;
}

export async function GET(request: Request) {
  const origin = new URL(request.url).origin;
  const response = NextResponse.redirect(new URL("/", request.url));
  response.cookies.set("swena_session", "", { path: "/", maxAge: 0 });
  return response;
}

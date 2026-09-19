import type { Metadata } from "next";
import "./globals.css";
import { Navbar } from "@/components/layout/Navbar";
import { Footer } from "@/components/layout/Footer";
import { SmoothScrollProvider } from "@/components/providers/SmoothScrollProvider";
import { AuthProvider } from "@/components/providers/AuthProvider";

export const metadata: Metadata = {
  title: "SWENA — India-First Travel Intelligence | We Gave You Memory",
  description:
    "Luxury multi-modal travel planning, optimization, and comparison platform for India. Zero hallucinations, constraint-solved scheduling, and authentic external booking handoff.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="antialiased bg-[#0D1915] text-[#F7F7F2]">
        <AuthProvider>
          <SmoothScrollProvider>
            <Navbar />
            <main className="min-h-screen pt-20">{children}</main>
            <Footer />
          </SmoothScrollProvider>
        </AuthProvider>
      </body>
    </html>
  );
}

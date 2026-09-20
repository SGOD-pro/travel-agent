import type { Metadata } from "next";
import "./globals.css";
import { Navbar } from "@/components/layout/Navbar";
import { Footer } from "@/components/layout/Footer";
import { SmoothScrollProvider } from "@/components/providers/SmoothScrollProvider";
import { AuthProvider } from "@/components/providers/AuthProvider";
import { Bodoni_Moda, Manrope } from "next/font/google";
import { cn } from "@/lib/utils";

const bodoni = Bodoni_Moda({
  subsets: ["latin"],
  variable: "--font-serif",
  display: "swap",
});

const manrope = Manrope({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

export const metadata: Metadata = {
  metadataBase: new URL("https://swena.travel"),
  title: {
    default: "SWENA — India-First Travel Intelligence | We Gave You Memory",
    template: "%s | SWENA Travel Intelligence",
  },
  description:
    "India-first multi-modal travel planning, optimization, and comparison platform. Zero hallucinations, constraint-solved road & rail scheduling, transparent budget calculations, and authentic direct supplier handoff.",
  keywords: [
    "India travel planner",
    "Western Ghats road trip",
    "Rajasthan heritage itinerary",
    "Konkan coast driving route",
    "India road trip planner",
    "constraint solved travel",
    "IRCTC direct handoff",
    "transparent travel budget",
  ],
  authors: [{ name: "SWENA Intelligence" }],
  creator: "SWENA Technologies",
  publisher: "SWENA",
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      "max-video-preview": -1,
      "max-image-preview": "large",
      "max-snippet": -1,
    },
  },
  openGraph: {
    type: "website",
    locale: "en_IN",
    url: "https://swena.travel",
    siteName: "SWENA Travel Intelligence",
    title: "SWENA — India-First Travel Intelligence | We Gave You Memory",
    description:
      "Bring driving routes, daily stops, and transparent budget assumptions into one calm workspace. Zero hallucinations, direct supplier handoff.",
    images: [
      {
        url: "/images/destinations/western-ghats.jpg",
        width: 1200,
        height: 630,
        alt: "SWENA Indian Travel Corridors — Western Ghats Tea Estates",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "SWENA — India-First Travel Intelligence",
    description:
      "Bring driving routes, daily stops, and transparent budget assumptions into one calm workspace. Zero hallucinations, direct supplier handoff.",
    images: ["/images/destinations/western-ghats.jpg"],
  },
};

const organizationJsonLd = {
  "@context": "https://schema.org",
  "@type": "Organization",
  name: "SWENA",
  legalName: "SWENA Travel Intelligence Technologies",
  url: "https://swena.travel",
  logo: "https://swena.travel/images/destinations/western-ghats.jpg",
  description:
    "India-first travel intelligence and multi-modal route planning platform with zero hallucinations and direct official merchant handoffs.",
  address: {
    "@type": "PostalAddress",
    streetAddress: "Indiranagar",
    addressLocality: "Bengaluru",
    addressRegion: "Karnataka",
    postalCode: "560038",
    addressCountry: "IN",
  },
  contactPoint: {
    "@type": "ContactPoint",
    email: "concierge@swena.travel",
    contactType: "customer service",
    availableLanguage: ["English", "Hindi", "Kannada"],
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={cn("font-sans", bodoni.variable, manrope.variable)}>
      <body className="antialiased bg-[#0D1915] text-[#F7F7F2]">
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(organizationJsonLd) }}
        />
        <AuthProvider>
          <SmoothScrollProvider>
            <Navbar />
            <main id="main-content" className="min-h-screen">{children}</main>
            <Footer />
          </SmoothScrollProvider>
        </AuthProvider>
      </body>
    </html>
  );
}

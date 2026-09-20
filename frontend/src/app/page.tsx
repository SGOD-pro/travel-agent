import type { Metadata } from "next";
import { HeroJourney } from "@/components/marketing/HeroJourney";
import { DestinationFilmstrip } from "@/components/marketing/DestinationFilmstrip";
import { EditorialPause } from "@/components/marketing/EditorialPause";
import { TripExample } from "@/components/marketing/TripExample";
import { PracticalFAQ } from "@/components/marketing/PracticalFAQ";
import { ClosingScene } from "@/components/marketing/ClosingScene";

export const metadata: Metadata = {
  title: "SWENA — India-First Travel Intelligence | The Landscape Becomes Your Journey",
  description:
    "India-first multi-modal travel planning, optimization, and comparison platform. Zero hallucinations, constraint-solved road & rail scheduling, transparent budget calculations, and authentic direct supplier handoff.",
  alternates: {
    canonical: "/",
  },
  openGraph: {
    title: "SWENA — The Landscape Becomes Your Journey",
    description:
      "Bring your driving route, daily stops, and transparent budget assumptions into one editable plan. Western Ghats, Rajasthan, and Konkan Coast circuits.",
    url: "https://swena.travel",
    type: "website",
    images: [
      {
        url: "/images/destinations/western-ghats.jpg",
        width: 1200,
        height: 630,
        alt: "SWENA Travel Intelligence — Western Ghats Tea Estates",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "SWENA — The Landscape Becomes Your Journey",
    description:
      "Bring your driving route, daily stops, and transparent budget assumptions into one editable plan.",
    images: ["/images/destinations/western-ghats.jpg"],
  },
};

export default function HomePage() {
  return (
    <div className="relative bg-[#0D1915] text-[#F7F7F2] selection:bg-[#B7C9AD] selection:text-[#102D25]">
      {/* Scene 1: Arrival & Landscape-to-Route Hero */}
      <HeroJourney />

      {/* Scene 2: Destination Filmstrip */}
      <DestinationFilmstrip />

      {/* Scene 3: Editorial Pause */}
      <EditorialPause />

      {/* Scene 4: The Plan, Made Tangible */}
      <TripExample />

      {/* Scene 5: Practical Reassurance FAQ */}
      <PracticalFAQ />

      {/* Scene 6: Closing Scene */}
      <ClosingScene />
    </div>
  );
}

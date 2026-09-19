import type { Metadata } from "next";
import Link from "next/link";
import Image from "next/image";
import {
  ShieldCheck,
  Cpu,
  Lock,
  ArrowRight,
  Database,
  Layers,
  Compass,
  MapPin,
  Clock,
  Sparkles,
  Mountain,
  Eye,
  CheckCircle2,
  FileSearch,
} from "lucide-react";

export const metadata: Metadata = {
  title: "About Our Philosophy & Architecture",
  description:
    "Why SWENA was built: restoring mathematical honesty, daylight realism, zero hallucinations, and official external booking handoffs to Indian travel planning.",
  alternates: {
    canonical: "/about",
  },
  openGraph: {
    title: "About SWENA — India-First Travel Intelligence",
    description:
      "Restoring mathematical honesty, daylight realism, and non-custodial booking handoff to Indian travel planning.",
    url: "https://swena.travel/about",
    type: "article",
    images: [
      {
        url: "/images/destinations/western-ghats.jpg",
        width: 1200,
        height: 630,
        alt: "SWENA Travel Intelligence — Western Ghats Corridors",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "About SWENA — India-First Travel Intelligence",
    description:
      "Restoring mathematical honesty, daylight realism, and non-custodial booking handoff to Indian travel planning.",
    images: ["/images/destinations/western-ghats.jpg"],
  },
};

const aboutJsonLd = {
  "@context": "https://schema.org",
  "@type": "AboutPage",
  name: "About SWENA Travel Intelligence",
  url: "https://swena.travel/about",
  description:
    "SWENA is an India-first sovereign travel planning platform designed to eliminate aggregator hallucination and provide constraint-solved multi-modal itineraries.",
  publisher: {
    "@type": "Organization",
    name: "SWENA",
    url: "https://swena.travel",
  },
  mainEntity: {
    "@type": "Article",
    headline: "We Gave You Memory: The Architecture Behind SWENA",
    description:
      "A technical and philosophical overview of why Indian travel planning requires mathematical constraint solving, topographic road reality, and non-custodial supplier handoff.",
    author: {
      "@type": "Organization",
      name: "SWENA Engineering & Research",
    },
  },
};

export default function AboutPage() {
  return (
    <div className="relative min-h-screen bg-[#0D1915] text-[#F7F7F2]">
      {/* Structured SEO Data */}
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(aboutJsonLd) }}
      />

      {/* =========================================================================
          CHAPTER A1: EDITORIAL HEADER
          ========================================================================= */}
      <section className="relative pt-24 pb-20 px-6 sm:px-12 border-b border-[#233E32] overflow-hidden">
        <div className="mx-auto max-w-4xl text-center space-y-6">
          <div className="inline-flex items-center gap-2 px-3.5 py-1 rounded-full bg-[#15271F] border border-[#B7C9AD]/20 text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider">
            <Compass className="h-3.5 w-3.5" />
            <span>Our Vision & Philosophy</span>
          </div>

          <h1 className="text-4xl sm:text-6xl lg:text-7xl font-bold tracking-tight text-[#F7F7F2] leading-[1.05]">
            We gave you <br className="hidden sm:inline" />
            <span className="text-[#B7C9AD]">memory.</span>
          </h1>

          <p className="text-base sm:text-xl text-[#A9B8AD] leading-relaxed max-w-2xl mx-auto font-normal">
            Indian travel planning has long been trapped between rigid tour packages, deceptive aggregator countdowns, and AI tools that treat mountain ghats as straight lines on a map.
          </p>

          <p className="text-sm sm:text-base text-[#6E8274] leading-relaxed max-w-xl mx-auto">
            SWENA was built to restore mathematical honesty, topographic realism, and calm focus to your journeys.
          </p>
        </div>
      </section>

      {/* =========================================================================
          CHAPTER A2: THE THREE SYSTEMIC FAILURES WE SET OUT TO FIX
          ========================================================================= */}
      <section className="py-20 px-6 sm:px-12 border-b border-[#233E32] bg-[#0A1411]">
        <div className="mx-auto max-w-5xl space-y-12">
          <div className="text-center max-w-2xl mx-auto space-y-3">
            <span className="text-xs font-semibold tracking-wider text-[#B7C9AD] uppercase">
              The Reality Gap
            </span>
            <h2 className="text-3xl sm:text-4xl font-bold text-[#F7F7F2]">
              Why traditional travel planning fails in India.
            </h2>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-6 space-y-3">
              <div className="h-10 w-10 rounded-xl bg-[#0D1915] border border-[#B7C9AD]/20 flex items-center justify-center text-[#B7C9AD]">
                <Eye className="h-5 w-5" />
              </div>
              <h3 className="text-lg font-bold text-[#F7F7F2]">
                1. The Aggregator Illusion
              </h3>
              <p className="text-xs sm:text-sm text-[#A9B8AD] leading-relaxed">
                Online travel agencies optimize for commission, creating artificial urgency, hidden mandatory resort fees, and toll plazas falsely zeroed out to make packages look cheap.
              </p>
            </div>

            <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-6 space-y-3">
              <div className="h-10 w-10 rounded-xl bg-[#0D1915] border border-[#B7C9AD]/20 flex items-center justify-center text-[#B7C9AD]">
                <Mountain className="h-5 w-5" />
              </div>
              <h3 className="text-lg font-bold text-[#F7F7F2]">
                2. The Topographic Blindspot
              </h3>
              <p className="text-xs sm:text-sm text-[#A9B8AD] leading-relaxed">
                Generic mapping apps and LLMs calculate distance using Euclidean geometry, promising 3-hour trips through monsoon-slick ghats that actually require 7 hours of cautious daylight navigation.
              </p>
            </div>

            <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-6 space-y-3">
              <div className="h-10 w-10 rounded-xl bg-[#0D1915] border border-[#B7C9AD]/20 flex items-center justify-center text-[#B7C9AD]">
                <Lock className="h-5 w-5" />
              </div>
              <h3 className="text-lg font-bold text-[#F7F7F2]">
                3. The Middleman Lock-In
              </h3>
              <p className="text-xs sm:text-sm text-[#A9B8AD] leading-relaxed">
                Intermediary portals hold your bookings hostage, charging hefty cancellation markups and making direct communication with station masters, homestays, or drivers impossible.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* =========================================================================
          CHAPTER A3: THE FOUR PILLARS OF SWENA
          ========================================================================= */}
      <section className="py-24 px-6 sm:px-12 border-b border-[#233E32]">
        <div className="mx-auto max-w-5xl space-y-16">
          <div className="text-center max-w-2xl mx-auto space-y-3">
            <span className="text-xs font-semibold tracking-wider text-[#B7C9AD] uppercase">
              Our Core Invariants
            </span>
            <h2 className="text-3xl sm:text-4xl font-bold text-[#F7F7F2]">
              Four non-negotiable principles.
            </h2>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
            <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-8 space-y-4">
              <div className="h-12 w-12 rounded-xl bg-[#0D1915] border border-[#B7C9AD]/30 flex items-center justify-center text-[#B7C9AD]">
                <ShieldCheck className="h-6 w-6" />
              </div>
              <h3 className="text-xl font-bold text-[#F7F7F2]">
                The Zero-Hallucination Standard
              </h3>
              <p className="text-sm text-[#A9B8AD] leading-relaxed">
                Zero fabricated room rates, invented train quotas, synthetic reviews, or fake booking IDs. Every number presented in SWENA belongs to one of four strict evidence classes: <span className="text-[#B7C9AD]">LIVE_OFFER</span>, <span className="text-[#B7C9AD]">INDICATIVE_SEARCH</span>, <span className="text-[#B7C9AD]">EDITORIAL_DISCOVERY</span>, or <span className="text-[#B7C9AD]">ESTIMATED_MODEL</span>.
              </p>
            </div>

            <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-8 space-y-4">
              <div className="h-12 w-12 rounded-xl bg-[#0D1915] border border-[#B7C9AD]/30 flex items-center justify-center text-[#B7C9AD]">
                <Database className="h-6 w-6" />
              </div>
              <h3 className="text-xl font-bold text-[#F7F7F2]">
                Non-Coercion of Unknown Costs
              </h3>
              <p className="text-sm text-[#A9B8AD] leading-relaxed">
                Unknown costs are never zero. If highway toll plazas, ghat corridor permits, or seasonal park safari fees are unverified, our budget arithmetic preserves them as explicit unknowns rather than fabricating compliance.
              </p>
            </div>

            <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-8 space-y-4">
              <div className="h-12 w-12 rounded-xl bg-[#0D1915] border border-[#B7C9AD]/30 flex items-center justify-center text-[#B7C9AD]">
                <Cpu className="h-6 w-6" />
              </div>
              <h3 className="text-xl font-bold text-[#F7F7F2]">
                Constraint Solvers Over Guesswork
              </h3>
              <p className="text-sm text-[#A9B8AD] leading-relaxed">
                Itineraries are formulated using Google OR-Tools solving the Traveling Salesperson Problem with Time Windows (TSPTW). Stop opening hours, geographic sequence, and daylight driving boundaries are enforced mathematically.
              </p>
            </div>

            <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-8 space-y-4">
              <div className="h-12 w-12 rounded-xl bg-[#0D1915] border border-[#B7C9AD]/30 flex items-center justify-center text-[#B7C9AD]">
                <Lock className="h-6 w-6" />
              </div>
              <h3 className="text-xl font-bold text-[#F7F7F2]">
                Direct Official Supplier Handoff
              </h3>
              <p className="text-sm text-[#A9B8AD] leading-relaxed">
                SWENA is strictly non-custodial. We hold zero traveler funds, capture zero card numbers, and create zero vouchers. When you are ready to book, we hand you directly to official merchant portals (IRCTC, airlines, registered heritage homestays).
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* =========================================================================
          CHAPTER A4: TOPOGRAPHIC REALITY & INDIAN CORRIDORS
          ========================================================================= */}
      <section className="py-24 px-6 sm:px-12 border-b border-[#233E32] bg-[#F7F7F2] text-[#0D1915]">
        <div className="mx-auto max-w-5xl space-y-12">
          <div className="space-y-4 max-w-3xl">
            <span className="text-xs font-bold tracking-widest text-[#6E8274] uppercase">
              India-First Geographic Depth
            </span>
            <h2 className="text-3xl sm:text-5xl font-bold tracking-tight text-[#0D1915] leading-[1.1]">
              Grounded in the geography of the subcontinent.
            </h2>
            <p className="text-base sm:text-lg text-[#33443B] leading-relaxed font-normal">
              India cannot be planned with generic North American grid assumptions. We model ghat road elevations, seasonal monsoon road conditions, and authentic dwell paces.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-8 pt-6">
            <div className="space-y-4">
              <div className="relative h-48 w-full rounded-2xl overflow-hidden border border-[#DDDCD3]">
                <Image
                  src="/images/destinations/western-ghats.jpg"
                  alt="Western Ghats tea plantations and mist"
                  fill
                  className="object-cover"
                  sizes="(max-width: 768px) 100vw, 33vw"
                />
              </div>
              <h3 className="text-xl font-bold text-[#0D1915]">Western Ghats</h3>
              <p className="text-xs sm:text-sm text-[#4F6055] leading-relaxed">
                Elevation profiles from 900m to 2,240m across Madikeri, Wayanad, and Munnar. Real daylight driving limits ensure mountain hairpins are navigated before evening mist rolls in.
              </p>
            </div>

            <div className="space-y-4">
              <div className="relative h-48 w-full rounded-2xl overflow-hidden border border-[#DDDCD3]">
                <Image
                  src="/images/destinations/rajasthan.jpg"
                  alt="Amber Fort sandstone courtyards in Jaipur"
                  fill
                  className="object-cover"
                  sizes="(max-width: 768px) 100vw, 33vw"
                />
              </div>
              <h3 className="text-xl font-bold text-[#0D1915]">Rajasthan Circuits</h3>
              <p className="text-xs sm:text-sm text-[#4F6055] leading-relaxed">
                Historic Aravalli highways connecting Delhi, Jaipur, Pushkar, and Jodhpur. Stop durations account for high-noon desert temperatures and morning courtyard visits.
              </p>
            </div>

            <div className="space-y-4">
              <div className="relative h-48 w-full rounded-2xl overflow-hidden border border-[#DDDCD3]">
                <Image
                  src="/images/destinations/konkan-coast.jpg"
                  alt="Arabian Sea coastline and palm groves along the Konkan coast"
                  fill
                  className="object-cover"
                  sizes="(max-width: 768px) 100vw, 33vw"
                />
              </div>
              <h3 className="text-xl font-bold text-[#0D1915]">Konkan Coastline</h3>
              <p className="text-xs sm:text-sm text-[#4F6055] leading-relaxed">
                Coastal ferry crossings, red dirt highways, and Arabian Sea headlands from Mumbai down to Goa. Calibrated for unhurried seafood lunches and coastal sunset viewpoints.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* =========================================================================
          CHAPTER A5: ENGINEERING & ARCHITECTURE TRANSPARENCY
          ========================================================================= */}
      <section className="py-24 px-6 sm:px-12 border-b border-[#233E32]">
        <div className="mx-auto max-w-5xl space-y-12">
          <div className="text-center max-w-2xl mx-auto space-y-3">
            <span className="text-xs font-semibold tracking-wider text-[#B7C9AD] uppercase">
              Architectural Rigor
            </span>
            <h2 className="text-3xl sm:text-4xl font-bold text-[#F7F7F2]">
              Engineered with clean boundaries.
            </h2>
          </div>

          <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-8 sm:p-10 space-y-6">
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6 text-xs">
              <div className="p-4 rounded-xl bg-[#0D1915] border border-[#233E32] space-y-1">
                <span className="text-[#6E8274] block uppercase font-bold text-[10px]">Orchestration</span>
                <span className="font-bold text-[#F7F7F2] text-sm block">LangGraph</span>
                <p className="text-[#A9B8AD]">Human-in-the-loop workflow state machine and proposal checkpoints.</p>
              </div>

              <div className="p-4 rounded-xl bg-[#0D1915] border border-[#233E32] space-y-1">
                <span className="text-[#6E8274] block uppercase font-bold text-[10px]">Optimization</span>
                <span className="font-bold text-[#F7F7F2] text-sm block">Google OR-Tools</span>
                <p className="text-[#A9B8AD]">Constraint-based TSPTW solver for mathematically optimal sequencing.</p>
              </div>

              <div className="p-4 rounded-xl bg-[#0D1915] border border-[#233E32] space-y-1">
                <span className="text-[#6E8274] block uppercase font-bold text-[10px]">Web Extraction</span>
                <span className="font-bold text-[#F7F7F2] text-sm block">Scrapling Engine</span>
                <p className="text-[#A9B8AD]">Resilient, typed indicative search extraction without synthetic hallucinations.</p>
              </div>

              <div className="p-4 rounded-xl bg-[#0D1915] border border-[#233E32] space-y-1">
                <span className="text-[#6E8274] block uppercase font-bold text-[10px]">Persistence</span>
                <span className="font-bold text-[#F7F7F2] text-sm block">PostgreSQL + PostGIS</span>
                <p className="text-[#A9B8AD]">Authoritative spatial corridor querying with Upstash Redis ephemeral caching.</p>
              </div>
            </div>

            <p className="text-xs text-[#6E8274] text-center pt-2">
              Identity governed by SWYRA OAuth 2.1 • Zero client-supplied owner IDs • Fail-closed authorization boundaries
            </p>
          </div>
        </div>
      </section>

      {/* =========================================================================
          CHAPTER A6: FINAL INVITATION
          ========================================================================= */}
      <section className="py-20 px-6 sm:px-12 text-center">
        <div className="mx-auto max-w-3xl space-y-6">
          <h2 className="text-3xl sm:text-5xl font-bold tracking-tight text-[#F7F7F2]">
            Ready to plan your next memory?
          </h2>
          <p className="text-base sm:text-lg text-[#A9B8AD] max-w-xl mx-auto">
            Experience travel intelligence that respects your time, your money, and the road ahead.
          </p>
          <div className="pt-4 flex flex-wrap items-center justify-center gap-4">
            <Link
              href="/dashboard"
              className="inline-flex items-center gap-2 rounded-xl bg-[#B7C9AD] px-7 py-3.5 text-sm font-semibold text-[#102D25] hover:bg-[#C9DBBE] transition-all shadow-lg"
            >
              <span>Open Trip Planner</span>
              <ArrowRight className="h-4 w-4" />
            </Link>
            <Link
              href="/contact"
              className="inline-flex items-center gap-2 rounded-xl bg-[#15271F] border border-[#233E32] px-6 py-3.5 text-sm font-medium text-[#F7F7F2] hover:bg-[#15271F]/80 transition-colors"
            >
              <span>Contact Concierge</span>
            </Link>
          </div>
        </div>
      </section>
    </div>
  );
}

"use client";

import Link from "next/link";
import {
  Compass,
  ShieldCheck,
  Cpu,
  Lock,
  Heart,
  Globe2,
  Sparkles,
  ArrowRight,
  Database,
  Layers,
} from "lucide-react";

export default function AboutPage() {
  return (
    <div className="relative py-16 px-6 max-w-5xl mx-auto">
      {/* Header */}
      <div className="text-center max-w-3xl mx-auto mb-20">
        <div className="inline-flex items-center gap-2 px-3.5 py-1 rounded-full bg-[#15271F] border border-[#B7C9AD]/20 text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider mb-6">
          <Heart className="h-3.5 w-3.5" />
          <span>Our Vision & Philosophy</span>
        </div>
        <h1 className="text-4xl sm:text-6xl font-bold tracking-tight text-[#F7F7F2]">
          We gave you <span className="text-[#B7C9AD]">memory.</span>
        </h1>
        <p className="mt-6 text-base sm:text-lg text-[#A9B8AD] leading-relaxed">
          Travel planning in India has long been crippled by deceptive inventory
          locks, fabricated aggregator prices, and AI tools that treat complex
          subcontinental topography as simple straight lines. SWENA exists to
          restore mathematical honesty and genuine peace of mind to travel.
        </p>
      </div>

      {/* Core Tenets */}
      <div className="space-y-12 mb-24">
        <h2 className="text-2xl sm:text-3xl font-bold text-[#F7F7F2] text-center mb-12">
          The Four Tenets of SWENA
        </h2>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
          <div className="glass-card p-8 rounded-3xl border border-[#233e32]">
            <div className="h-12 w-12 rounded-2xl bg-[#15271F] border border-[#B7C9AD]/20 flex items-center justify-center text-[#B7C9AD] mb-6">
              <ShieldCheck className="h-6 w-6" />
            </div>
            <h3 className="text-xl font-bold text-[#F7F7F2]">
              1. The Zero-Hallucination Standard
            </h3>
            <p className="text-sm text-[#A9B8AD] mt-3 leading-relaxed">
              We never fabricate a room rate, train berth availability, or
              monument entry fee. If real-time evidence cannot be verified from an
              official supplier, we label it as unverified rather than inventing
              synthetic reality.
            </p>
          </div>

          <div className="glass-card p-8 rounded-3xl border border-[#233e32]">
            <div className="h-12 w-12 rounded-2xl bg-[#15271F] border border-[#B7C9AD]/20 flex items-center justify-center text-[#B7C9AD] mb-6">
              <Database className="h-6 w-6" />
            </div>
            <h3 className="text-xl font-bold text-[#F7F7F2]">
              2. Non-Coercion of Unknown Costs
            </h3>
            <p className="text-sm text-[#A9B8AD] mt-3 leading-relaxed">
              When an expressway toll plaza or high-altitude permit cost is unquoted,
              we do not coerce it to ₹0 to claim false budget compliance. We state
              the unknown explicitly so you are never caught unprepared.
            </p>
          </div>

          <div className="glass-card p-8 rounded-3xl border border-[#233e32]">
            <div className="h-12 w-12 rounded-2xl bg-[#15271F] border border-[#B7C9AD]/20 flex items-center justify-center text-[#B7C9AD] mb-6">
              <Cpu className="h-6 w-6" />
            </div>
            <h3 className="text-xl font-bold text-[#F7F7F2]">
              3. Constraint Solvers over Guesswork
            </h3>
            <p className="text-sm text-[#A9B8AD] mt-3 leading-relaxed">
              Itinerary scheduling is handled by Google OR-Tools solving the
              Traveling Salesperson Problem with Time Windows (TSPTW). Multi-day
              stops, visit durations, and road transit times advance logically
              without overlapping.
            </p>
          </div>

          <div className="glass-card p-8 rounded-3xl border border-[#233e32]">
            <div className="h-12 w-12 rounded-2xl bg-[#15271F] border border-[#B7C9AD]/20 flex items-center justify-center text-[#B7C9AD] mb-6">
              <Lock className="h-6 w-6" />
            </div>
            <h3 className="text-xl font-bold text-[#F7F7F2]">
              4. External Official Handoff
            </h3>
            <p className="text-sm text-[#A9B8AD] mt-3 leading-relaxed">
              We do not act as an opaque middleman. You maintain full ownership
              of your bookings by checking out directly on verified airline,
              IRCTC rail, and hotel portals with verified deep-links.
            </p>
          </div>
        </div>
      </div>

      {/* Engineering Heritage */}
      <div className="glass-panel p-10 rounded-3xl border border-[#B7C9AD]/20 mb-20">
        <h2 className="text-2xl font-bold text-[#F7F7F2] mb-6 flex items-center gap-3">
          <Layers className="h-6 w-6 text-[#B7C9AD]" />
          <span>Under the Hood</span>
        </h2>
        <div className="space-y-4 text-sm text-[#A9B8AD] leading-relaxed">
          <p>
            SWENA is engineered with a shared modular backend architecture in Python 3.12,
            orchestrated by <span className="text-[#F7F7F2] font-semibold">LangGraph</span> for
            deterministic workflow state management and human-in-the-loop review.
          </p>
          <p>
            Spatial identity and transit coordinates are stored authoritatively in{" "}
            <span className="text-[#F7F7F2] font-semibold">Aiven PostgreSQL with PostGIS</span>,
            with high-performance ephemeral caching handled by{" "}
            <span className="text-[#F7F7F2] font-semibold">Upstash Redis</span>.
          </p>
        </div>
      </div>

      {/* CTA */}
      <div className="text-center">
        <Link
          href="/dashboard"
          className="btn-sage inline-flex items-center gap-2 px-8 py-4 rounded-xl text-base font-semibold shadow-xl"
        >
          <Sparkles className="h-5 w-5" />
          <span>Try the Planner Now</span>
          <ArrowRight className="h-5 w-5 ml-1" />
        </Link>
      </div>
    </div>
  );
}

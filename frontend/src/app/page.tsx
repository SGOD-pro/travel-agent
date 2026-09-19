"use client";

import React, { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowRight,
  ShieldCheck,
  Cpu,
  MapPin,
  Sparkles,
  Gauge,
  Compass,
  CheckCircle2,
  AlertCircle,
  Car,
  Bike,
  Route,
  Navigation,
} from "lucide-react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { TravelCard3D } from "@/components/ui/travel-card-3d";
import { useRouter } from "next/navigation";

export default function HomePage() {
  const router = useRouter();
  const heroRef = useRef<HTMLDivElement>(null);
  const headlineRef = useRef<HTMLHeadingElement>(null);
  const subheadRef = useRef<HTMLParagraphElement>(null);
  const corridorsRef = useRef<HTMLDivElement>(null);
  const interactiveDemoRef = useRef<HTMLDivElement>(null);

  // Interactive demo state
  const [selectedVehicle, setSelectedVehicle] = useState<"car" | "bike">("car");
  const [selectedCorridor, setSelectedCorridor] = useState<number>(0);

  const sampleCorridors = [
    {
      name: "Western Ghats Corridor",
      route: "Bengaluru → Mysuru → Coorg → Wayanad",
      distanceKm: 340,
      durationHours: 6.5,
      carMileage: 15.0,
      bikeMileage: 40.0,
      fuelPrice: 102.5,
      tollsVerified: false,
      tollReason: "Expressway toll rates unverified for two-wheeler bypass",
      knownTolls: 320,
    },
    {
      name: "Royal Heritage Odyssey",
      route: "Delhi → Agra → Jaipur → Jodhpur",
      distanceKm: 610,
      durationHours: 10.2,
      carMileage: 16.5,
      bikeMileage: 42.0,
      fuelPrice: 96.7,
      tollsVerified: true,
      knownTolls: 890,
    },
    {
      name: "Deccan Plateau & Konkan Coast",
      route: "Mumbai → Pune → Mahabaleshwar → Goa",
      distanceKm: 580,
      durationHours: 11.0,
      carMileage: 14.5,
      bikeMileage: 38.0,
      fuelPrice: 104.2,
      tollsVerified: false,
      tollReason: "Ghat section seasonal pass rates unverified",
      knownTolls: 640,
    },
  ];

  const currentCorridor = sampleCorridors[selectedCorridor];
  const mileage =
    selectedVehicle === "car"
      ? currentCorridor.carMileage
      : currentCorridor.bikeMileage;
  const fuelLiters = (currentCorridor.distanceKm / mileage).toFixed(1);
  const fuelCost = Math.round(
    parseFloat(fuelLiters) * currentCorridor.fuelPrice
  );

  useEffect(() => {
    gsap.registerPlugin(ScrollTrigger);

    const ctx = gsap.context(() => {
      // Cinematic hero fade and rise
      const tl = gsap.timeline();
      tl.from(headlineRef.current, {
        y: 40,
        opacity: 0,
        duration: 1.1,
        ease: "power3.out",
      }).from(
        subheadRef.current,
        {
          y: 25,
          opacity: 0,
          duration: 0.9,
          ease: "power3.out",
        },
        "-=0.6"
      );

      // Scroll triggered reveal for corridors
      if (corridorsRef.current) {
        gsap.from(corridorsRef.current.children, {
          scrollTrigger: {
            trigger: corridorsRef.current,
            start: "top 80%",
          },
          y: 45,
          opacity: 0,
          stagger: 0.15,
          duration: 0.8,
          ease: "power2.out",
        });
      }
    }, heroRef);

    return () => ctx.revert();
  }, []);

  return (
    <div ref={heroRef} className="relative overflow-hidden">
      {/* Ambient background glow accents (Forest Night palette) */}
      <div className="absolute top-0 left-1/2 -translate-x-1/2 w-[1000px] h-[550px] bg-[#15271F] blur-[150px] rounded-full -z-10 opacity-70 pointer-events-none" />
      <div className="absolute top-[800px] left-0 w-[600px] h-[600px] bg-[#15271F]/40 blur-[180px] rounded-full -z-10 pointer-events-none" />

      {/* SECTION 1: CINEMATIC HERO */}
      <section className="relative px-6 pt-16 pb-28 sm:pt-24 sm:pb-36 max-w-7xl mx-auto text-center">
        <div className="inline-flex items-center gap-2.5 px-4 py-1.5 rounded-full bg-[#15271F] border border-[#B7C9AD]/20 text-xs font-semibold text-[#B7C9AD] tracking-wide uppercase mb-8 shadow-sm">
          <Sparkles className="h-3.5 w-3.5" />
          <span>SWENA 2.0 • India-First Multi-Modal Travel Platform</span>
        </div>

        <h1
          ref={headlineRef}
          className="text-5xl sm:text-7xl lg:text-8xl font-bold tracking-tight text-[#F7F7F2] max-w-5xl mx-auto leading-[1.08]"
        >
          We gave you <span className="text-[#B7C9AD]">memory.</span>
        </h1>

        <p
          ref={subheadRef}
          className="mt-8 text-lg sm:text-xl text-[#A9B8AD] max-w-3xl mx-auto leading-relaxed font-normal"
        >
          The luxury travel intelligence platform built on mathematical certainty.
          Multi-stop route sequencing solved via Google OR-Tools, zero-hallucination
          pricing, and uncoerced road transit budgeting for India.
        </p>

        {/* Hero CTAs */}
        <div className="mt-12 flex flex-col sm:flex-row items-center justify-center gap-4">
          <Link
            href="/dashboard"
            className="btn-sage w-full sm:w-auto flex items-center justify-center gap-3 px-8 py-4 rounded-xl text-base font-semibold shadow-xl"
          >
            <span>Launch Itinerary Planner</span>
            <ArrowRight className="h-5 w-5" />
          </Link>
          <Link
            href="/about"
            className="btn-outline-forest w-full sm:w-auto flex items-center justify-center gap-2 px-8 py-4 rounded-xl text-base font-semibold"
          >
            <span>Explore the Ethos</span>
          </Link>
        </div>

        {/* Feature badges strip */}
        <div className="mt-20 grid grid-cols-2 md:grid-cols-4 gap-4 max-w-5xl mx-auto">
          <div className="glass-card p-5 rounded-2xl text-left">
            <ShieldCheck className="h-6 w-6 text-[#B7C9AD] mb-3" />
            <h3 className="text-sm font-semibold text-[#F7F7F2]">0 Hallucinations</h3>
            <p className="text-xs text-[#A9B8AD] mt-1">
              Zero fabricated quotes, availability, or fake booking holds.
            </p>
          </div>

          <div className="glass-card p-5 rounded-2xl text-left">
            <Cpu className="h-6 w-6 text-[#B7C9AD] mb-3" />
            <h3 className="text-sm font-semibold text-[#F7F7F2]">OR-Tools Engine</h3>
            <p className="text-xs text-[#A9B8AD] mt-1">
              Constraint-based TSP solver with stay windows & arrival sequencing.
            </p>
          </div>

          <div className="glass-card p-5 rounded-2xl text-left">
            <Gauge className="h-6 w-6 text-[#B7C9AD] mb-3" />
            <h3 className="text-sm font-semibold text-[#F7F7F2]">Exact Transit Math</h3>
            <p className="text-xs text-[#A9B8AD] mt-1">
              Deterministic fuel calculations with uncoerced toll tracking.
            </p>
          </div>

          <div className="glass-card p-5 rounded-2xl text-left">
            <Navigation className="h-6 w-6 text-[#B7C9AD] mb-3" />
            <h3 className="text-sm font-semibold text-[#F7F7F2]">Direct Handoff</h3>
            <p className="text-xs text-[#A9B8AD] mt-1">
              Handoff to official merchants without intermediary markups.
            </p>
          </div>
        </div>
      </section>

      {/* SECTION 2: INTERACTIVE LIVE CORRIDOR ENGINE */}
      <section
        ref={interactiveDemoRef}
        className="px-6 py-20 max-w-7xl mx-auto border-t border-[#15271F]"
      >
        <div className="text-center max-w-3xl mx-auto mb-14">
          <div className="inline-flex items-center gap-2 text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider mb-3">
            <Route className="h-4 w-4" />
            <span>Deterministic Transit Engine</span>
          </div>
          <h2 className="text-3xl sm:text-4xl font-bold text-[#F7F7F2]">
            Experience Real-Time Indian Road Mathematics
          </h2>
          <p className="mt-3 text-sm sm:text-base text-[#A9B8AD]">
            Toggle vehicle profiles and corridors. Observe how unverified toll
            data is treated with strict mathematical honesty rather than coerced to zero.
          </p>
        </div>

        {/* Interactive Console */}
        <div className="glass-card rounded-3xl p-6 sm:p-10 max-w-5xl mx-auto border border-[#B7C9AD]/20 shadow-2xl">
          {/* Corridor selector pills */}
          <div className="flex flex-wrap items-center justify-center gap-2.5 mb-8">
            {sampleCorridors.map((corridor, idx) => (
              <button
                key={corridor.name}
                onClick={() => setSelectedCorridor(idx)}
                className={`px-4 py-2 rounded-xl text-xs sm:text-sm font-medium transition-all ${
                  selectedCorridor === idx
                    ? "bg-[#B7C9AD] text-[#102D25] font-semibold shadow-md"
                    : "bg-[#15271F] text-[#A9B8AD] hover:text-[#F7F7F2] border border-[#233e32]"
                }`}
              >
                {corridor.name}
              </button>
            ))}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-8 items-center">
            {/* Left: Corridor details & Vehicle Switch */}
            <div className="space-y-6">
              <div>
                <span className="text-xs text-[#A9B8AD] uppercase tracking-wider">
                  Active Corridor
                </span>
                <h3 className="text-xl font-bold text-[#F7F7F2] mt-1">
                  {currentCorridor.name}
                </h3>
                <p className="text-xs text-[#B7C9AD] font-mono mt-1">
                  {currentCorridor.route}
                </p>
              </div>

              <div>
                <span className="text-xs text-[#A9B8AD] uppercase tracking-wider block mb-2">
                  Vehicle Profile
                </span>
                <div className="grid grid-cols-2 gap-2 bg-[#0D1915] p-1.5 rounded-xl border border-[#233e32]">
                  <button
                    onClick={() => setSelectedVehicle("car")}
                    className={`flex items-center justify-center gap-2 py-2 px-3 rounded-lg text-xs font-semibold transition-all ${
                      selectedVehicle === "car"
                        ? "bg-[#15271F] text-[#B7C9AD] shadow-sm"
                        : "text-[#A9B8AD] hover:text-[#F7F7F2]"
                    }`}
                  >
                    <Car className="h-4 w-4" />
                    <span>Petrol Car</span>
                  </button>
                  <button
                    onClick={() => setSelectedVehicle("bike")}
                    className={`flex items-center justify-center gap-2 py-2 px-3 rounded-lg text-xs font-semibold transition-all ${
                      selectedVehicle === "bike"
                        ? "bg-[#15271F] text-[#B7C9AD] shadow-sm"
                        : "text-[#A9B8AD] hover:text-[#F7F7F2]"
                    }`}
                  >
                    <Bike className="h-4 w-4" />
                    <span>Motorcycle</span>
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3 text-xs">
                <div className="bg-[#0D1915]/60 p-3 rounded-xl border border-[#15271F]">
                  <span className="text-[#A9B8AD] block">Distance</span>
                  <span className="text-sm font-bold text-[#F7F7F2]">
                    {currentCorridor.distanceKm} km
                  </span>
                </div>
                <div className="bg-[#0D1915]/60 p-3 rounded-xl border border-[#15271F]">
                  <span className="text-[#A9B8AD] block">Benchmark</span>
                  <span className="text-sm font-bold text-[#B7C9AD]">
                    {mileage} km/L
                  </span>
                </div>
              </div>
            </div>

            {/* Middle: Real-time math breakdown */}
            <div className="bg-[#0D1915] p-6 rounded-2xl border border-[#233e32] space-y-4">
              <h4 className="text-xs font-semibold uppercase tracking-wider text-[#A9B8AD] flex items-center gap-2">
                <Gauge className="h-4 w-4 text-[#B7C9AD]" />
                <span>Computed Fuel Math</span>
              </h4>

              <div className="space-y-3 font-mono text-xs">
                <div className="flex justify-between py-1 border-b border-[#15271F]">
                  <span className="text-[#A9B8AD]">Fuel Required:</span>
                  <span className="text-[#F7F7F2] font-semibold">
                    {fuelLiters} Liters
                  </span>
                </div>
                <div className="flex justify-between py-1 border-b border-[#15271F]">
                  <span className="text-[#A9B8AD]">Avg Rate:</span>
                  <span className="text-[#F7F7F2] font-semibold">
                    ₹{currentCorridor.fuelPrice}/L
                  </span>
                </div>
                <div className="flex justify-between py-1 text-sm font-bold pt-2">
                  <span className="text-[#B7C9AD]">Fuel Subtotal:</span>
                  <span className="text-[#B7C9AD]">₹{fuelCost.toLocaleString()}</span>
                </div>
              </div>
            </div>

            {/* Right: Integrity & Zero-Coercion Status */}
            <div className="space-y-4">
              <div
                className={`p-5 rounded-2xl border ${
                  currentCorridor.tollsVerified
                    ? "bg-[#15271F]/60 border-[#B7C9AD]/30"
                    : "bg-[#271E15]/60 border-[#D4A373]/30"
                }`}
              >
                <div className="flex items-start gap-3">
                  {currentCorridor.tollsVerified ? (
                    <CheckCircle2 className="h-5 w-5 text-[#B7C9AD] shrink-0 mt-0.5" />
                  ) : (
                    <AlertCircle className="h-5 w-5 text-[#D4A373] shrink-0 mt-0.5" />
                  )}
                  <div>
                    <h5 className="text-xs font-semibold text-[#F7F7F2] uppercase tracking-wide">
                      {currentCorridor.tollsVerified
                        ? "Budget Status: Complete"
                        : "Budget Status: Incomplete (Preserved)"}
                    </h5>
                    <p className="text-xs text-[#A9B8AD] mt-1 leading-relaxed">
                      {currentCorridor.tollsVerified
                        ? `Official highway toll charges verified: ₹${currentCorridor.knownTolls}.`
                        : `${currentCorridor.tollReason}. Cost is not coerced to zero.`}
                    </p>
                  </div>
                </div>
              </div>

              <div className="bg-[#15271F] p-4 rounded-xl border border-[#233e32] flex items-center justify-between">
                <span className="text-xs text-[#A9B8AD]">Estimated Road Transit:</span>
                <span className="text-base font-bold text-[#F7F7F2]">
                  ₹{(fuelCost + (currentCorridor.tollsVerified ? currentCorridor.knownTolls : 0)).toLocaleString()}
                  {!currentCorridor.tollsVerified && <span className="text-xs text-[#D4A373] ml-1 font-normal">+ unknown</span>}
                </span>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* SECTION 2.5: FEATURED 3D CORRIDOR SHOWCASE (GSAP POWERED) */}
      <section className="px-6 py-20 max-w-7xl mx-auto border-t border-[#15271F]">
        <div className="text-center max-w-3xl mx-auto mb-14">
          <div className="inline-flex items-center gap-2 text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider mb-3">
            <Sparkles className="h-4 w-4" />
            <span>Interactive 3D Corridor Discovery</span>
          </div>
          <h2 className="text-3xl sm:text-4xl font-bold text-[#F7F7F2]">
            Signature Topographic Corridors
          </h2>
          <p className="mt-3 text-sm sm:text-base text-[#A9B8AD]">
            Hover to experience depth-layered 3D interaction powered by GSAP.
            Inspect ground truth elevations, verified transit routes, and regional advisories.
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-8 justify-items-center">
          <TravelCard3D
            title="Western Ghats Monsoon Odyssey"
            subtitle="Bengaluru → Coorg → Wayanad → Ooty"
            tagline="Elevation: 900m – 2,240m • 340 km"
            badge="Biodiversity Hotspot"
            verified={true}
            actionText="Explore Ghats Route"
            onActionClick={() => router.push("/trips/sample-western-ghats-corridor")}
          />
          <TravelCard3D
            title="Royal Rajputana Circuit"
            subtitle="Delhi → Jaipur → Jodhpur → Udaipur"
            tagline="Heritage Expressways • 610 km"
            badge="Desert & Fort Corridor"
            verified={true}
            actionText="Explore Royal Route"
            onActionClick={() => router.push("/trips/sample-rajasthan-heritage-circuit")}
          />
          <TravelCard3D
            title="Konkan & Sahyadri Pass"
            subtitle="Mumbai → Pune → Mahabaleshwar → Goa"
            tagline="Coastal Ghat Curves • 580 km"
            badge="Coastal Transit"
            verified={true}
            actionText="Explore Coastal Route"
            onActionClick={() => router.push("/trips/sample-goa-monsoon-coastal-escape")}
          />
        </div>
      </section>

      {/* SECTION 3: ARCHITECTURAL PILLARS */}
      <section className="px-6 py-24 max-w-7xl mx-auto">
        <div className="text-center max-w-3xl mx-auto mb-16">
          <span className="text-xs font-semibold uppercase tracking-wider text-[#B7C9AD]">
            Engineered for Precision
          </span>
          <h2 className="text-3xl sm:text-5xl font-bold text-[#F7F7F2] mt-3">
            Why Generic AI Itineraries Fail Travelers
          </h2>
          <p className="text-sm sm:text-base text-[#A9B8AD] mt-4">
            Most AI travel tools hallucinate fictitious flight schedules, ignore
            corridor topography, and pretend entry fees are zero. SWENA operates as
            a verifiable deterministic engine.
          </p>
        </div>

        <div ref={corridorsRef} className="grid grid-cols-1 md:grid-cols-3 gap-8">
          <div className="glass-card p-8 rounded-3xl border border-[#233e32]">
            <div className="h-12 w-12 rounded-2xl bg-[#15271F] border border-[#B7C9AD]/20 flex items-center justify-center text-[#B7C9AD] mb-6">
              <Cpu className="h-6 w-6" />
            </div>
            <h3 className="text-xl font-bold text-[#F7F7F2]">
              Mathematical Solvers vs Probabilistic Guesses
            </h3>
            <p className="text-sm text-[#A9B8AD] mt-3 leading-relaxed">
              We do not ask an LLM to guess driving durations. Google OR-Tools
              solves the traveling salesperson problem with time windows, guaranteeing
              arrival times advance logically without teleportation.
            </p>
          </div>

          <div className="glass-card p-8 rounded-3xl border border-[#233e32]">
            <div className="h-12 w-12 rounded-2xl bg-[#15271F] border border-[#B7C9AD]/20 flex items-center justify-center text-[#B7C9AD] mb-6">
              <ShieldCheck className="h-6 w-6" />
            </div>
            <h3 className="text-xl font-bold text-[#F7F7F2]">
              Strict Non-Coercion of Unknown Costs
            </h3>
            <p className="text-sm text-[#A9B8AD] mt-3 leading-relaxed">
              If a national park entry fee or expressway toll cannot be verified
              from an official source, it remains an explicit unknown. We never
              silently treat unknowns as ₹0 to claim false budget compliance.
            </p>
          </div>

          <div className="glass-card p-8 rounded-3xl border border-[#233e32]">
            <div className="h-12 w-12 rounded-2xl bg-[#15271F] border border-[#B7C9AD]/20 flex items-center justify-center text-[#B7C9AD] mb-6">
              <Compass className="h-6 w-6" />
            </div>
            <h3 className="text-xl font-bold text-[#F7F7F2]">
              Direct Official Merchant Handoff
            </h3>
            <p className="text-sm text-[#A9B8AD] mt-3 leading-relaxed">
              No intermediary markups, no deceptive hotel inventory locks, and no
              internal payment hostage taking. Review transparent evidence and book
              directly on IRCTC, airline, or hotel portals.
            </p>
          </div>
        </div>
      </section>

      {/* SECTION 4: CALL TO ACTION */}
      <section className="px-6 py-20 max-w-5xl mx-auto mb-20">
        <div className="glass-card p-10 sm:p-16 rounded-3xl border border-[#B7C9AD]/30 text-center relative overflow-hidden">
          <div className="absolute top-0 right-0 w-64 h-64 bg-[#B7C9AD]/10 blur-3xl rounded-full pointer-events-none" />
          <h2 className="text-3xl sm:text-5xl font-bold text-[#F7F7F2] max-w-2xl mx-auto">
            Ready to design a journey with true peace of mind?
          </h2>
          <p className="mt-4 text-base text-[#A9B8AD] max-w-xl mx-auto">
            Input your origin, preferred travel dates, and dream destinations.
            Watch SWENA compile a mathematically verified plan in seconds.
          </p>
          <div className="mt-8 flex justify-center">
            <Link
              href="/dashboard"
              className="btn-sage px-8 py-4 rounded-xl text-base font-semibold shadow-2xl flex items-center gap-2"
            >
              <Sparkles className="h-5 w-5" />
              <span>Open the Planning Dashboard</span>
            </Link>
          </div>
        </div>
      </section>
    </div>
  );
}

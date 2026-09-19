"use client";

import React, { useState, useRef, useEffect } from "react";
import Link from "next/link";
import Image from "next/image";
import {
  ArrowRight,
  Compass,
  Sparkles,
  MapPin,
  Clock,
  Calendar,
  ShieldCheck,
  CheckCircle2,
  AlertCircle,
  Car,
  ChevronRight,
  ExternalLink,
  HelpCircle,
} from "lucide-react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";

type DestinationKey = "western-ghats" | "rajasthan" | "konkan";

interface DestinationInfo {
  id: DestinationKey;
  label: string;
  locationKicker: string;
  image: string;
  alt: string;
  caption: string;
  corridorSlug: string;
  elevation: string;
  routeHighlight: string;
}

const DESTINATIONS: Record<DestinationKey, DestinationInfo> = {
  "western-ghats": {
    id: "western-ghats",
    label: "Western Ghats",
    locationKicker: "KARNATAKA & KERALA CORRIDORS",
    image: "/images/destinations/western-ghats.jpg",
    alt: "Misty green hills and tea plantations of the Western Ghats under morning light",
    caption: "Misty ridge lines, coffee country, and unhurried ghat roads from Bengaluru to Wayanad.",
    corridorSlug: "western-ghats",
    elevation: "900m – 2,240m",
    routeHighlight: "Bengaluru → Mysuru → Coorg → Wayanad",
  },
  rajasthan: {
    id: "rajasthan",
    label: "Rajasthan",
    locationKicker: "ROYAL RAJPUTANA CORRIDORS",
    image: "/images/destinations/rajasthan.jpg",
    alt: "Sunlit sandstone arches and royal courtyards of Amber Fort in Jaipur, Rajasthan",
    caption: "Sunlit stone courtyards, ancient stepwells, and wide desert highways between Jaipur and Jodhpur.",
    corridorSlug: "rajasthan",
    elevation: "260m – 430m",
    routeHighlight: "Delhi → Jaipur → Pushkar → Jodhpur",
  },
  konkan: {
    id: "konkan",
    label: "Konkan coast",
    locationKicker: "COASTAL MAHARASHTRA & GOA",
    image: "/images/destinations/konkan-coast.jpg",
    alt: "Arabian Sea waves washing against tropical palm-lined cliffs along the Konkan coastline",
    caption: "Red coastal roads, quiet palm coves, and sea air stretching from Mumbai down to Goa.",
    corridorSlug: "konkan-coast",
    elevation: "Sea level – 180m",
    routeHighlight: "Mumbai → Alibaug → Ratnagiri → Goa",
  },
};

export default function HomePage() {
  const [activeDest, setActiveDest] = useState<DestinationKey>("western-ghats");
  const [activeStep, setActiveStep] = useState<number>(1);
  const heroContentRef = useRef<HTMLDivElement>(null);

  const currentDest = DESTINATIONS[activeDest];

  // GSAP Entrance Animation
  useGSAP(
    () => {
      const mm = gsap.matchMedia();

      mm.add("(prefers-reduced-motion: no-preference)", () => {
        gsap.from(".hero-reveal", {
          y: 24,
          opacity: 0,
          duration: 0.85,
          stagger: 0.08,
          ease: "power2.out",
        });
      });

      return () => mm.revert();
    },
    { scope: heroContentRef }
  );

  return (
    <div className="relative bg-[#0D1915] text-[#F7F7F2] selection:bg-[#B7C9AD] selection:text-[#102D25]">
      {/* =========================================================================
          CHAPTER H1: DESTINATION-FIRST HERO
          ========================================================================= */}
      <section
        className="relative min-h-[90svh] flex flex-col justify-between overflow-hidden pt-24 pb-12 px-6 sm:px-12 border-b border-[#233E32]"
        aria-label="Hero: Indian travel planning"
      >
        {/* Layered Background Imagery with Crossfade */}
        <div className="absolute inset-0 z-0">
          {(Object.keys(DESTINATIONS) as DestinationKey[]).map((key) => {
            const dest = DESTINATIONS[key];
            const isActive = activeDest === key;
            return (
              <div
                key={dest.id}
                className={`absolute inset-0 transition-opacity duration-700 ease-out ${
                  isActive ? "opacity-100 scale-100" : "opacity-0 scale-102 pointer-events-none"
                }`}
                style={{ transitionProperty: "opacity, transform" }}
              >
                <Image
                  src={dest.image}
                  alt={dest.alt}
                  fill
                  priority={key === "western-ghats"}
                  className="object-cover object-center"
                  sizes="100vw"
                />
              </div>
            );
          })}

          {/* Cinematic Scrim: subtle contrast without blacking out the landscape */}
          <div className="absolute inset-0 bg-gradient-to-r from-[#0D1915]/95 via-[#0D1915]/75 to-transparent" />
          <div className="absolute inset-0 bg-gradient-to-t from-[#0D1915] via-transparent to-[#0D1915]/40" />
        </div>

        {/* Hero Narrative Content */}
        <div
          ref={heroContentRef}
          className="relative z-10 max-w-3xl my-auto pt-8 sm:pt-14 pb-8"
        >
          {/* Location Kicker */}
          <div className="hero-reveal inline-flex items-center gap-2 px-3 py-1 rounded-full bg-[#15271F]/80 border border-[#B7C9AD]/25 text-xs font-semibold text-[#B7C9AD] tracking-wider uppercase mb-6 backdrop-blur-sm">
            <span>{currentDest.locationKicker}</span>
            <span className="text-[#6E8274]">•</span>
            <span className="text-[#A9B8AD] font-normal">{currentDest.elevation}</span>
          </div>

          {/* Main Editorial Headline */}
          <h1 className="hero-reveal text-4xl sm:text-6xl lg:text-7xl font-bold tracking-tight text-[#F7F7F2] leading-[1.02] mb-6">
            Less planning. <br />
            <span className="text-[#B7C9AD]">More remembering.</span>
          </h1>

          {/* Supporting Copy */}
          <p className="hero-reveal text-base sm:text-xl text-[#A9B8AD] max-w-xl font-normal leading-relaxed mb-8">
            Bring your driving route, daily stops, and transparent budget assumptions into one editable trip plan.
          </p>

          {/* Primary & Secondary Actions */}
          <div className="hero-reveal flex flex-wrap items-center gap-4 mb-10">
            <Link
              href="/dashboard"
              className="inline-flex items-center gap-2 rounded-xl bg-[#B7C9AD] px-6 py-3.5 text-sm font-semibold text-[#102D25] hover:bg-[#C9DBBE] transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD] shadow-lg shadow-black/30 cursor-pointer"
            >
              <span>Plan my trip</span>
              <ArrowRight className="h-4 w-4" />
            </Link>

            <a
              href="#how-it-works"
              className="inline-flex items-center gap-2 rounded-xl bg-[#15271F]/80 border border-[#233E32] px-5 py-3.5 text-sm font-medium text-[#F7F7F2] hover:bg-[#15271F] hover:border-[#B7C9AD]/40 transition-colors backdrop-blur-sm"
            >
              Explore an example
            </a>
          </div>

          {/* Destination Selector Tabs */}
          <div className="hero-reveal pt-4 border-t border-[#233E32]/60">
            <div className="flex flex-wrap items-center gap-2" role="tablist" aria-label="Select destination corridor">
              <span className="text-xs uppercase tracking-wider text-[#6E8274] mr-2">
                Corridor:
              </span>
              {(Object.keys(DESTINATIONS) as DestinationKey[]).map((key) => {
                const isSelected = activeDest === key;
                return (
                  <button
                    key={key}
                    type="button"
                    role="tab"
                    aria-selected={isSelected}
                    onClick={() => setActiveDest(key)}
                    className={`px-3.5 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD] ${
                      isSelected
                        ? "bg-[#15271F] text-[#B7C9AD] border border-[#B7C9AD]/40 shadow-sm"
                        : "bg-[#0D1915]/60 text-[#A9B8AD] border border-[#233E32]/80 hover:text-[#F7F7F2] hover:bg-[#15271F]/40"
                    }`}
                  >
                    {DESTINATIONS[key].label}
                  </button>
                );
              })}
            </div>
            <p className="mt-3 text-xs text-[#A9B8AD] leading-relaxed max-w-lg">
              {currentDest.caption}
            </p>
          </div>
        </div>

        {/* Footer Note of Hero */}
        <div className="relative z-10 flex flex-col sm:flex-row items-start sm:items-center justify-between text-xs text-[#6E8274] pt-4 border-t border-[#233E32]/40 gap-2">
          <span>Official booking handoff to IRCTC, state tourism, and direct suppliers</span>
          <span className="hidden sm:inline">Zero payment capture or internal lock-in</span>
        </div>
      </section>

      {/* =========================================================================
          CHAPTER H2: EDITORIAL BRIDGE (WARM PAPER)
          ========================================================================= */}
      <section
        className="bg-[#F7F7F2] text-[#0D1915] py-20 sm:py-28 px-6 sm:px-12 border-b border-[#E2E2DC]"
        aria-label="Philosophy: Room for the unexpected"
      >
        <div className="mx-auto max-w-5xl">
          <div className="grid grid-cols-1 md:grid-cols-12 gap-8 md:gap-12 items-center">
            <div className="md:col-span-8 space-y-5">
              <span className="text-xs font-bold tracking-widest text-[#6E8274] uppercase">
                The SWENA Approach
              </span>
              <h2 className="text-3xl sm:text-5xl font-bold tracking-tight text-[#0D1915] leading-[1.12]">
                A good trip leaves room for the unexpected.
              </h2>
              <p className="text-base sm:text-lg text-[#33443B] leading-relaxed max-w-2xl font-normal">
                Most travel tools force you between rigid tour packages and dozens of disconnected browser tabs.
                SWENA brings your driving corridors, daily stops, and budget assumptions into one clear, editable plan—so
                you spend less time coordinating and more time taking in the morning light.
              </p>
            </div>

            <div className="md:col-span-4 bg-[#EDECE5] border border-[#DDDCD3] p-5 rounded-2xl space-y-3">
              <div className="flex items-center gap-2 text-xs font-semibold text-[#15271F] uppercase tracking-wider">
                <Compass className="h-4 w-4 text-[#2E5A44]" />
                <span>Representative Corridor</span>
              </div>
              <p className="text-sm font-bold text-[#0D1915]">
                Bengaluru → Mysuru → Coorg → Wayanad
              </p>
              <div className="text-xs text-[#526458] space-y-1 pt-2 border-t border-[#DDDCD3]">
                <div className="flex justify-between">
                  <span>Corridor Distance</span>
                  <span className="font-semibold text-[#0D1915]">340 km</span>
                </div>
                <div className="flex justify-between">
                  <span>Paced Driving Time</span>
                  <span className="font-semibold text-[#0D1915]">~6.5 hours</span>
                </div>
                <div className="flex justify-between">
                  <span>Road Terrain</span>
                  <span className="font-semibold text-[#0D1915]">Plain to Ghat Pass</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* =========================================================================
          CHAPTER H3: "WATCH A TRIP TAKE SHAPE" INTERACTIVE STORY (#how-it-works)
          ========================================================================= */}
      <section
        id="how-it-works"
        className="py-24 sm:py-32 px-6 sm:px-12 bg-[#0D1915] border-b border-[#233E32]"
        aria-label="Interactive Story: How a trip takes shape"
      >
        <div className="mx-auto max-w-6xl">
          {/* Section Header */}
          <div className="max-w-2xl mb-16 space-y-3">
            <span className="text-xs font-semibold tracking-wider text-[#B7C9AD] uppercase">
              How It Works
            </span>
            <h2 className="text-3xl sm:text-5xl font-bold tracking-tight text-[#F7F7F2]">
              Watch a trip take shape.
            </h2>
            <p className="text-base sm:text-lg text-[#A9B8AD]">
              From your initial destination ideas to an itemized, daylight-balanced daily schedule.
            </p>
          </div>

          {/* Desktop Two-Column Layout */}
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-10 items-start">
            {/* Left Steps Column (5 cols) */}
            <div className="lg:col-span-5 space-y-4">
              {[
                {
                  step: 1,
                  title: "Start with your kind of trip",
                  subtitle: "Destination, dates, travelers, pace",
                  body: "Tell us where you want to go and how you like to travel. We account for your vehicle type, party size, and daylight driving limits.",
                },
                {
                  step: 2,
                  title: "Find a rhythm that fits",
                  subtitle: "Day sequence, route, stop selections",
                  body: "Stops are arranged in a logical geographic flow. Dwell times and transit hours are paced so mornings stay unhurried.",
                },
                {
                  step: 3,
                  title: "See the details before you decide",
                  subtitle: "Cost categories, estimates, unknown items",
                  body: "Inspect estimated fuel and stay costs alongside unquoted tolls and park fees. We never hide unknown charges behind a zero.",
                },
              ].map((item) => {
                const isActive = activeStep === item.step;
                return (
                  <button
                    key={item.step}
                    type="button"
                    onClick={() => setActiveStep(item.step)}
                    className={`w-full text-left p-6 rounded-2xl transition-all cursor-pointer border ${
                      isActive
                        ? "bg-[#15271F] border-[#B7C9AD]/40 shadow-xl shadow-black/20"
                        : "bg-[#0D1915] border-[#233E32]/70 hover:border-[#233E32] hover:bg-[#15271F]/30"
                    }`}
                  >
                    <div className="flex items-center gap-3 mb-2">
                      <div
                        className={`flex h-7 w-7 items-center justify-center rounded-full text-xs font-bold ${
                          isActive
                            ? "bg-[#B7C9AD] text-[#102D25]"
                            : "bg-[#233E32] text-[#A9B8AD]"
                        }`}
                      >
                        {item.step}
                      </div>
                      <h3
                        className={`text-base font-bold ${
                          isActive ? "text-[#F7F7F2]" : "text-[#A9B8AD]"
                        }`}
                      >
                        {item.title}
                      </h3>
                    </div>
                    <p className="text-xs font-medium text-[#B7C9AD] mb-2 pl-10">
                      {item.subtitle}
                    </p>
                    <p className="text-sm text-[#A9B8AD] pl-10 leading-relaxed">
                      {item.body}
                    </p>
                  </button>
                );
              })}

              <div className="pt-4 pl-2">
                <Link
                  href="/dashboard"
                  className="inline-flex items-center gap-2 text-sm font-semibold text-[#B7C9AD] hover:text-[#C9DBBE] transition-colors"
                >
                  <span>Make it your trip in the planner</span>
                  <ArrowRight className="h-4 w-4" />
                </Link>
              </div>
            </div>

            {/* Right Interactive Preview Card (7 cols, sticky) */}
            <div className="lg:col-span-7 lg:sticky lg:top-28">
              <div className="rounded-2xl bg-[#15271F] border border-[#233E32] p-6 sm:p-8 shadow-2xl shadow-black/40 space-y-6">
                {/* Header Banner */}
                <div className="flex items-center justify-between pb-4 border-b border-[#233E32]">
                  <div className="flex items-center gap-2">
                    <span className="inline-block h-2 w-2 rounded-full bg-[#B7C9AD]" />
                    <span className="text-xs font-bold uppercase tracking-wider text-[#B7C9AD]">
                      Example Itinerary Preview
                    </span>
                  </div>
                  <span className="text-xs text-[#6E8274]">
                    Step {activeStep} of 3
                  </span>
                </div>

                {/* Dynamic Step Preview Content */}
                {activeStep === 1 && (
                  <div className="space-y-4">
                    <h4 className="text-lg font-bold text-[#F7F7F2]">
                      Initial Travel Brief Parameters
                    </h4>
                    <div className="grid grid-cols-2 gap-3 text-xs">
                      <div className="p-3.5 rounded-xl bg-[#0D1915] border border-[#233E32]">
                        <span className="text-[#6E8274] block mb-1">Origin & Corridor</span>
                        <span className="font-semibold text-[#F7F7F2]">Bengaluru → Coorg</span>
                      </div>
                      <div className="p-3.5 rounded-xl bg-[#0D1915] border border-[#233E32]">
                        <span className="text-[#6E8274] block mb-1">Duration</span>
                        <span className="font-semibold text-[#F7F7F2]">3 Days • 2 Nights</span>
                      </div>
                      <div className="p-3.5 rounded-xl bg-[#0D1915] border border-[#233E32]">
                        <span className="text-[#6E8274] block mb-1">Travelers</span>
                        <span className="font-semibold text-[#F7F7F2]">2 Adults (1 Room)</span>
                      </div>
                      <div className="p-3.5 rounded-xl bg-[#0D1915] border border-[#233E32]">
                        <span className="text-[#6E8274] block mb-1">Vehicle Mode</span>
                        <span className="font-semibold text-[#F7F7F2]">Petrol Car (15 km/L)</span>
                      </div>
                    </div>
                    <div className="p-4 rounded-xl bg-[#0D1915]/60 border border-[#233E32]/70 text-xs text-[#A9B8AD] leading-relaxed">
                      <span className="font-semibold text-[#B7C9AD]">Daylight Bound:</span> Departure set for 06:30 AM to pass Mysore before midday traffic and reach Madikeri ridge before dusk.
                    </div>
                  </div>
                )}

                {activeStep === 2 && (
                  <div className="space-y-4">
                    <h4 className="text-lg font-bold text-[#F7F7F2]">
                      Balanced Daily Schedule
                    </h4>
                    <div className="space-y-2.5 text-xs">
                      <div className="p-3 rounded-xl bg-[#0D1915] border border-[#233E32] flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          <span className="font-mono text-[#B7C9AD] font-semibold">06:30 AM</span>
                          <span className="text-[#F7F7F2] font-medium">Bengaluru Departure</span>
                        </div>
                        <span className="text-[#6E8274]">Origin Hub</span>
                      </div>
                      <div className="p-3 rounded-xl bg-[#0D1915] border border-[#233E32] flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          <span className="font-mono text-[#B7C9AD] font-semibold">10:15 AM</span>
                          <div>
                            <span className="text-[#F7F7F2] font-medium block">Mysuru Palace & Heritage Zone</span>
                            <span className="text-[#6E8274] text-[11px]">240 min dwell • Heritage visit</span>
                          </div>
                        </div>
                        <span className="text-[#B7C9AD] font-medium">Midday Stop</span>
                      </div>
                      <div className="p-3 rounded-xl bg-[#0D1915] border border-[#233E32] flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          <span className="font-mono text-[#B7C9AD] font-semibold">05:45 PM</span>
                          <div>
                            <span className="text-[#F7F7F2] font-medium block">Madikeri, Coorg</span>
                            <span className="text-[#6E8274] text-[11px]">Estate Homestay check-in</span>
                          </div>
                        </div>
                        <span className="text-[#B7C9AD] font-medium">Day 1 Destination</span>
                      </div>
                    </div>
                  </div>
                )}

                {activeStep === 3 && (
                  <div className="space-y-4">
                    <h4 className="text-lg font-bold text-[#F7F7F2]">
                      Itemized Cost & Unknown Item Disclosure
                    </h4>
                    <div className="space-y-2 text-xs">
                      <div className="flex justify-between py-2 border-b border-[#233E32]/60">
                        <span className="text-[#A9B8AD]">Estimated Fuel (340 km @ 15 km/L @ ₹102.5/L)</span>
                        <span className="font-mono font-semibold text-[#F7F7F2]">₹2,323.00</span>
                      </div>
                      <div className="flex justify-between py-2 border-b border-[#233E32]/60">
                        <span className="text-[#A9B8AD]">Verified Highway Tolls</span>
                        <span className="font-mono font-semibold text-[#F7F7F2]">₹320.00</span>
                      </div>
                      <div className="flex justify-between py-2 border-b border-[#233E32]/60">
                        <span className="text-[#A9B8AD]">Estimated Lodging (2 nights, 1 room)</span>
                        <span className="font-mono font-semibold text-[#F7F7F2]">₹7,000.00</span>
                      </div>
                      <div className="flex justify-between py-2 border-b border-[#233E32]/60 items-center">
                        <span className="text-[#A9B8AD]">Ghat Corridor Entry Permit</span>
                        <span className="px-2 py-0.5 rounded bg-amber-500/10 text-amber-300 font-medium text-[11px]">
                          Unknown fee (Not added to total)
                        </span>
                      </div>
                    </div>

                    <div className="p-3.5 rounded-xl bg-[#0D1915] border border-[#233E32] flex items-center justify-between">
                      <div>
                        <span className="text-[11px] text-[#6E8274] block">Summary Standard</span>
                        <span className="text-xs font-semibold text-[#B7C9AD]">
                          Known subtotal: ₹9,643.00; permit fee unknown
                        </span>
                      </div>
                      <span className="text-xs text-[#6E8274] italic">Zero coercion</span>
                    </div>
                  </div>
                )}

                {/* Bottom Card Controls */}
                <div className="flex items-center justify-between pt-4 border-t border-[#233E32] text-xs">
                  <span className="text-[#6E8274]">
                    *Illustrative values; actuals compile on server
                  </span>
                  <div className="flex gap-2">
                    {[1, 2, 3].map((step) => (
                      <button
                        key={step}
                        type="button"
                        onClick={() => setActiveStep(step)}
                        className={`h-2 w-6 rounded-full transition-all ${
                          activeStep === step ? "bg-[#B7C9AD]" : "bg-[#233E32]"
                        }`}
                        aria-label={`View step ${step} preview`}
                      />
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* =========================================================================
          CHAPTER H4: DESTINATION JOURNAL (#journeys)
          ========================================================================= */}
      <section
        id="journeys"
        className="py-24 sm:py-32 px-6 sm:px-12 bg-[#0A1411] border-b border-[#233E32]"
        aria-label="Destination Journal: Where memory begins"
      >
        <div className="mx-auto max-w-6xl">
          <div className="max-w-2xl mb-16 space-y-3">
            <span className="text-xs font-semibold tracking-wider text-[#B7C9AD] uppercase">
              Inspiration Corridors
            </span>
            <h2 className="text-3xl sm:text-5xl font-bold tracking-tight text-[#F7F7F2]">
              Where will your next memory begin?
            </h2>
            <p className="text-base sm:text-lg text-[#A9B8AD]">
              Three distinct Indian landscapes, each with its own character, driving rhythm, and morning light.
            </p>
          </div>

          {/* Asymmetric Editorial Spread */}
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-stretch">
            {/* Feature 1: Large Landscape (7 cols) */}
            <div className="lg:col-span-7 flex flex-col justify-between rounded-2xl bg-[#15271F] border border-[#233E32] overflow-hidden group">
              <div className="relative h-72 sm:h-96 w-full overflow-hidden">
                <Image
                  src="/images/destinations/western-ghats.jpg"
                  alt="Misty green hills of the Western Ghats"
                  fill
                  className="object-cover group-hover:scale-103 transition-transform duration-700 ease-out"
                  sizes="(max-width: 1024px) 100vw, 60vw"
                />
                <div className="absolute inset-0 bg-gradient-to-t from-[#15271F] via-transparent to-transparent" />
                <div className="absolute top-4 left-4 px-3 py-1 rounded-full bg-[#0D1915]/80 backdrop-blur-sm border border-[#B7C9AD]/20 text-xs font-semibold text-[#B7C9AD]">
                  Western Ghats
                </div>
              </div>
              <div className="p-6 sm:p-8 space-y-4">
                <h3 className="text-2xl font-bold text-[#F7F7F2]">
                  Into the Western Ghats
                </h3>
                <p className="text-sm text-[#A9B8AD] leading-relaxed">
                  Coffee country, green hills, slower mornings. From the Mysore plateau into the mist of Madikeri and Wayanad, winding through spice estates and quiet mountain passes.
                </p>
                <div className="pt-2 flex items-center justify-between">
                  <Link
                    href="/dashboard"
                    className="inline-flex items-center gap-2 text-xs font-semibold text-[#B7C9AD] hover:text-[#C9DBBE] transition-colors"
                  >
                    <span>Use this inspiration in planner</span>
                    <ArrowRight className="h-3.5 w-3.5" />
                  </Link>
                  <span className="text-xs text-[#6E8274]">340 km corridor</span>
                </div>
              </div>
            </div>

            {/* Feature 2 & 3: Stacked Offset Cards (5 cols) */}
            <div className="lg:col-span-5 space-y-8 flex flex-col justify-between">
              {/* Card 2: Rajasthan */}
              <div className="rounded-2xl bg-[#15271F] border border-[#233E32] overflow-hidden group flex-1 flex flex-col justify-between">
                <div className="relative h-48 w-full overflow-hidden">
                  <Image
                    src="/images/destinations/rajasthan.jpg"
                    alt="Courtyards of Amber Fort, Rajasthan"
                    fill
                    className="object-cover group-hover:scale-103 transition-transform duration-700 ease-out"
                    sizes="(max-width: 1024px) 100vw, 40vw"
                  />
                  <div className="absolute inset-0 bg-gradient-to-t from-[#15271F] via-transparent to-transparent" />
                  <div className="absolute top-3 left-3 px-2.5 py-0.5 rounded-full bg-[#0D1915]/80 backdrop-blur-sm border border-[#B7C9AD]/20 text-xs font-semibold text-[#B7C9AD]">
                    Rajasthan
                  </div>
                </div>
                <div className="p-6 space-y-2">
                  <h3 className="text-xl font-bold text-[#F7F7F2]">
                    Through Rajasthan
                  </h3>
                  <p className="text-xs text-[#A9B8AD] leading-relaxed">
                    Old stone cities, courtyards, changing landscapes. From Delhi through Jaipur to Jodhpur across the historic Aravalli corridors.
                  </p>
                  <div className="pt-2">
                    <Link
                      href="/dashboard"
                      className="inline-flex items-center gap-1.5 text-xs font-semibold text-[#B7C9AD] hover:text-[#C9DBBE]"
                    >
                      <span>Use this inspiration</span>
                      <ArrowRight className="h-3 w-3" />
                    </Link>
                  </div>
                </div>
              </div>

              {/* Card 3: Konkan Coast */}
              <div className="rounded-2xl bg-[#15271F] border border-[#233E32] overflow-hidden group flex-1 flex flex-col justify-between">
                <div className="relative h-48 w-full overflow-hidden">
                  <Image
                    src="/images/destinations/konkan-coast.jpg"
                    alt="Arabian Sea coastline along Konkan"
                    fill
                    className="object-cover group-hover:scale-103 transition-transform duration-700 ease-out"
                    sizes="(max-width: 1024px) 100vw, 40vw"
                  />
                  <div className="absolute inset-0 bg-gradient-to-t from-[#15271F] via-transparent to-transparent" />
                  <div className="absolute top-3 left-3 px-2.5 py-0.5 rounded-full bg-[#0D1915]/80 backdrop-blur-sm border border-[#B7C9AD]/20 text-xs font-semibold text-[#B7C9AD]">
                    Konkan Coast
                  </div>
                </div>
                <div className="p-6 space-y-2">
                  <h3 className="text-xl font-bold text-[#F7F7F2]">
                    Along the Konkan coast
                  </h3>
                  <p className="text-xs text-[#A9B8AD] leading-relaxed">
                    Coastal towns, sea air, unhurried stops. Coastal highway stretches, ferry crossings, and quiet palm groves along the Arabian Sea.
                  </p>
                  <div className="pt-2">
                    <Link
                      href="/dashboard"
                      className="inline-flex items-center gap-1.5 text-xs font-semibold text-[#B7C9AD] hover:text-[#C9DBBE]"
                    >
                      <span>Use this inspiration</span>
                      <ArrowRight className="h-3 w-3" />
                    </Link>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* =========================================================================
          CHAPTER H5: USEFUL PLANNING EXPLAINED THROUGH THE PRODUCT
          ========================================================================= */}
      <section
        className="py-24 sm:py-32 px-6 sm:px-12 bg-[#0D1915] border-b border-[#233E32]"
        aria-label="Planning capabilities explained through product"
      >
        <div className="mx-auto max-w-5xl space-y-20">
          <div className="max-w-2xl space-y-3">
            <span className="text-xs font-semibold tracking-wider text-[#B7C9AD] uppercase">
              Honest By Design
            </span>
            <h2 className="text-3xl sm:text-5xl font-bold tracking-tight text-[#F7F7F2]">
              Useful planning, without the usual noise.
            </h2>
          </div>

          {/* Row 1: Change the plan. Keep the trip. */}
          <div className="grid grid-cols-1 md:grid-cols-12 gap-8 items-center">
            <div className="md:col-span-6 space-y-4">
              <span className="text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider">
                01 • Dynamic Adjustment
              </span>
              <h3 className="text-2xl sm:text-3xl font-bold text-[#F7F7F2]">
                Change the plan. Keep the trip.
              </h3>
              <p className="text-sm sm:text-base text-[#A9B8AD] leading-relaxed">
                Plans change on the road. When you decide to stay an extra morning in coffee country or take an unhurried detour to an ancient temple, SWENA recomputes transit times and adjusts adjacent legs without wiping out your entire itinerary.
              </p>
            </div>
            <div className="md:col-span-6 p-6 rounded-2xl bg-[#15271F] border border-[#233E32] space-y-3">
              <div className="flex items-center justify-between text-xs text-[#A9B8AD] pb-2 border-b border-[#233E32]">
                <span>Stop Duration Adjustment</span>
                <span className="text-[#B7C9AD] font-semibold">Recalculated</span>
              </div>
              <div className="text-xs space-y-2">
                <div className="p-3 rounded-lg bg-[#0D1915] border border-[#233E32] flex justify-between items-center">
                  <span>Madikeri Homestay</span>
                  <span className="text-[#B7C9AD] font-semibold">+1 Day Extended</span>
                </div>
                <div className="p-3 rounded-lg bg-[#0D1915] border border-[#233E32] flex justify-between items-center text-[#A9B8AD]">
                  <span>Subsequent Transit to Wayanad</span>
                  <span>Shifted to Day 3, 09:30 AM</span>
                </div>
              </div>
            </div>
          </div>

          {/* Row 2: Know what is included—and what is still an estimate. */}
          <div className="grid grid-cols-1 md:grid-cols-12 gap-8 items-center">
            <div className="md:col-span-6 md:order-2 space-y-4">
              <span className="text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider">
                02 • Financial Transparency
              </span>
              <h3 className="text-2xl sm:text-3xl font-bold text-[#F7F7F2]">
                Know what is included—and what is still an estimate.
              </h3>
              <p className="text-sm sm:text-base text-[#A9B8AD] leading-relaxed">
                Unknown highway tolls, seasonal park permits, and unquoted entry fees never quietly become zero. We itemize what is verified, what is modeled, and what is still unknown, so your budget means what it says.
              </p>
            </div>
            <div className="md:col-span-6 md:order-1 p-6 rounded-2xl bg-[#15271F] border border-[#233E32] space-y-3">
              <div className="text-xs font-bold text-[#B7C9AD] uppercase tracking-wider pb-2 border-b border-[#233E32]">
                Budget Integrity Standard
              </div>
              <div className="space-y-2 text-xs">
                <div className="flex justify-between p-2 rounded bg-[#0D1915]">
                  <span className="text-[#A9B8AD]">Fuel & Sourced Lodging</span>
                  <span className="font-semibold text-[#F7F7F2]">₹9,320.00 (Modeled)</span>
                </div>
                <div className="flex justify-between p-2 rounded bg-[#0D1915]">
                  <span className="text-[#A9B8AD]">National Highway Tolls</span>
                  <span className="font-semibold text-[#F7F7F2]">₹320.00 (Verified)</span>
                </div>
                <div className="flex justify-between p-2 rounded bg-amber-500/10 border border-amber-500/20 text-amber-300">
                  <span>State Forest Entry Fee</span>
                  <span>Unquoted (Marked Unknown)</span>
                </div>
              </div>
            </div>
          </div>

          {/* Row 3: Choose where you book. */}
          <div className="grid grid-cols-1 md:grid-cols-12 gap-8 items-center">
            <div className="md:col-span-6 space-y-4">
              <span className="text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider">
                03 • Supplier Sovereignty
              </span>
              <h3 className="text-2xl sm:text-3xl font-bold text-[#F7F7F2]">
                Choose where you book.
              </h3>
              <p className="text-sm sm:text-base text-[#A9B8AD] leading-relaxed">
                SWENA is a planning and comparison workspace, not a checkout broker. When you are ready to reserve a train, flight, or hotel, we provide direct handoff links to official portals like IRCTC and state tourism sites. No markups, no hidden locks.
              </p>
            </div>
            <div className="md:col-span-6 p-6 rounded-2xl bg-[#15271F] border border-[#233E32] space-y-3">
              <div className="text-xs font-bold text-[#B7C9AD] uppercase tracking-wider pb-2 border-b border-[#233E32]">
                Official Handoff Example
              </div>
              <div className="p-4 rounded-xl bg-[#0D1915] border border-[#233E32] flex items-center justify-between text-xs">
                <div className="space-y-1">
                  <span className="font-semibold text-[#F7F7F2] block">Vande Bharat Express (Train 20608)</span>
                  <span className="text-[#6E8274]">Deep link pre-filled with date & passenger count</span>
                </div>
                <div className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg bg-[#15271F] text-[#B7C9AD] border border-[#B7C9AD]/30 font-medium">
                  <span>View on IRCTC</span>
                  <ExternalLink className="h-3 w-3" />
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* =========================================================================
          CHAPTER H6: PRACTICAL FAQ
          ========================================================================= */}
      <section
        className="py-24 sm:py-32 px-6 sm:px-12 bg-[#0A1411] border-b border-[#233E32]"
        aria-label="Frequently Asked Questions"
      >
        <div className="mx-auto max-w-4xl space-y-12">
          <div className="text-center space-y-3 max-w-xl mx-auto">
            <span className="text-xs font-semibold tracking-wider text-[#B7C9AD] uppercase">
              Practical Information
            </span>
            <h2 className="text-3xl sm:text-4xl font-bold tracking-tight text-[#F7F7F2]">
              Frequently asked questions.
            </h2>
            <p className="text-sm text-[#A9B8AD]">
              Straight answers about how SWENA plans journeys, handles pricing, and connects you to suppliers.
            </p>
          </div>

          <Accordion type="single" collapsible className="w-full space-y-3">
            <AccordionItem value="faq-1" className="border border-[#233E32] rounded-xl px-5 bg-[#15271F]">
              <AccordionTrigger className="text-sm font-semibold text-[#F7F7F2] hover:text-[#B7C9AD]">
                How does SWENA generate an itinerary?
              </AccordionTrigger>
              <AccordionContent className="text-xs sm:text-sm text-[#A9B8AD] leading-relaxed pt-2">
                SWENA uses constraint-based optimization to sequence your daily stops, estimate transit times, and balance driving durations against daylight hours. Your brief sets the hard rules (destinations, dates, party, vehicle mode); the system helps you find a realistic daily rhythm without overlapping visits.
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="faq-2" className="border border-[#233E32] rounded-xl px-5 bg-[#15271F]">
              <AccordionTrigger className="text-sm font-semibold text-[#F7F7F2] hover:text-[#B7C9AD]">
                Does SWENA take payments or book tickets directly?
              </AccordionTrigger>
              <AccordionContent className="text-xs sm:text-sm text-[#A9B8AD] leading-relaxed pt-2">
                No. SWENA is strictly non-custodial. We hold zero customer funds and create zero tickets. When you are ready to book, we provide direct handoff links to verified official portals such as IRCTC, airlines, and registered hotel sites so you transact directly with the supplier.
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="faq-3" className="border border-[#233E32] rounded-xl px-5 bg-[#15271F]">
              <AccordionTrigger className="text-sm font-semibold text-[#F7F7F2] hover:text-[#B7C9AD]">
                Are all displayed prices live and guaranteed?
              </AccordionTrigger>
              <AccordionContent className="text-xs sm:text-sm text-[#A9B8AD] leading-relaxed pt-2">
                Prices on SWENA reflect their verified evidence status. Live provider quotes carry a checked timestamp and expiry window. When rates are unverified, we label them as estimates or explicit unknowns rather than inventing a fixed price hold.
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="faq-4" className="border border-[#233E32] rounded-xl px-5 bg-[#15271F]">
              <AccordionTrigger className="text-sm font-semibold text-[#F7F7F2] hover:text-[#B7C9AD]">
                Can I change stops or driving pace after creating a plan?
              </AccordionTrigger>
              <AccordionContent className="text-xs sm:text-sm text-[#A9B8AD] leading-relaxed pt-2">
                Yes. Every itinerary is fully editable. You can reorder stops, adjust dwell durations, change your fuel mileage assumptions, or remove destinations. SWENA recomputes only the affected travel legs and updates your version history.
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="faq-5" className="border border-[#233E32] rounded-xl px-5 bg-[#15271F]">
              <AccordionTrigger className="text-sm font-semibold text-[#F7F7F2] hover:text-[#B7C9AD]">
                Which regions of India are currently supported?
              </AccordionTrigger>
              <AccordionContent className="text-xs sm:text-sm text-[#A9B8AD] leading-relaxed pt-2">
                SWENA is optimized for Indian domestic road and rail corridors, with curated topographic routing across the Western Ghats (Karnataka and Kerala), Rajasthan heritage circuits, and the Konkan coast. Additional regional corridors are added as road network datasets are verified.
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </div>
      </section>

      {/* =========================================================================
          CHAPTER H7: FINAL INVITATION
          ========================================================================= */}
      <section
        className="relative py-28 sm:py-36 px-6 sm:px-12 text-center overflow-hidden border-b border-[#233E32]"
        aria-label="Call to action: Start your journey"
      >
        <div className="absolute inset-0 z-0">
          <Image
            src="/images/destinations/western-ghats.jpg"
            alt="Misty landscape of Western Ghats"
            fill
            className="object-cover object-center brightness-30"
            sizes="100vw"
          />
          <div className="absolute inset-0 bg-[#0D1915]/80 backdrop-blur-[2px]" />
        </div>

        <div className="relative z-10 max-w-2xl mx-auto space-y-6">
          <span className="text-xs font-semibold tracking-widest text-[#B7C9AD] uppercase">
            Start Your Journey
          </span>
          <h2 className="text-3xl sm:text-5xl font-bold tracking-tight text-[#F7F7F2] leading-tight">
            Make room for the journey.
          </h2>
          <p className="text-base sm:text-lg text-[#A9B8AD] max-w-lg mx-auto leading-relaxed">
            Less planning. More remembering. Bring your route, stays, and budget into one calm workspace.
          </p>
          <div className="pt-4">
            <Link
              href="/dashboard"
              className="inline-flex items-center gap-2 rounded-xl bg-[#B7C9AD] px-7 py-3.5 text-sm font-semibold text-[#102D25] hover:bg-[#C9DBBE] transition-all shadow-xl shadow-black/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD]"
            >
              <span>Plan my trip</span>
              <ArrowRight className="h-4 w-4" />
            </Link>
          </div>
        </div>
      </section>
    </div>
  );
}

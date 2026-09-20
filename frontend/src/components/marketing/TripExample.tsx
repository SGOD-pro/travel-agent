"use client";

import React, { useState } from "react";
import Link from "next/link";
import {
  ArrowRight,
  Clock,
  Compass,
  Calendar,
  Sparkles,
  CheckCircle2,
  AlertCircle,
  HelpCircle,
} from "lucide-react";

type PaceMode = "unhurried" | "balanced" | "indepth";

interface PaceDetails {
  id: PaceMode;
  label: string;
  days: number;
  nights: number;
  description: string;
  daylightGuidance: string;
  lodgingEstimate: number;
  stops: {
    day: number;
    time: string;
    location: string;
    activity: string;
    dwell: string;
    distance: string;
  }[];
}

const PACE_FIXTURES: Record<PaceMode, PaceDetails> = {
  unhurried: {
    id: "unhurried",
    label: "Unhurried",
    days: 3,
    nights: 2,
    description: "Later departures, longer cafe mornings, maximum 2 stops per day.",
    daylightGuidance: "Departures after 08:30 AM; check-in before 05:00 PM.",
    lodgingEstimate: 6400,
    stops: [
      {
        day: 1,
        time: "08:30 AM",
        location: "Bengaluru → Mysuru",
        activity: "Mysore Palace courtyard & lunch",
        dwell: "240 min dwell",
        distance: "145 km (~3.2 hrs)",
      },
      {
        day: 2,
        time: "09:45 AM",
        location: "Mysuru → Coorg (Madikeri)",
        activity: "Plantation homestay check-in & coffee walk",
        dwell: "Overnight estate stay",
        distance: "115 km (~2.8 hrs)",
      },
      {
        day: 3,
        time: "10:30 AM",
        location: "Coorg → Wayanad",
        activity: "Ghat viewpoint & spice plantation visit",
        dwell: "180 min dwell",
        distance: "80 km (~2.2 hrs)",
      },
    ],
  },
  balanced: {
    id: "balanced",
    label: "Balanced",
    days: 4,
    nights: 3,
    description: "Daylight-bound transit with afternoon arrivals and balanced exploration.",
    daylightGuidance: "Departures at 07:00 AM; pass Mysore expressway before midday heat.",
    lodgingEstimate: 9200,
    stops: [
      {
        day: 1,
        time: "07:00 AM",
        location: "Bengaluru → Mysuru",
        activity: "Devaraja Market & Mysore Palace",
        dwell: "180 min dwell",
        distance: "145 km (~2.7 hrs)",
      },
      {
        day: 2,
        time: "08:30 AM",
        location: "Mysuru → Nagarhole → Coorg",
        activity: "Kabini river buffer route to Madikeri",
        dwell: "120 min buffer dwell",
        distance: "135 km (~3.2 hrs)",
      },
      {
        day: 3,
        time: "09:00 AM",
        location: "Coorg (Madikeri)",
        activity: "Abbey Falls, coffee cupping & estate trail",
        dwell: "Full day dwell",
        distance: "30 km local (~1 hr)",
      },
      {
        day: 4,
        time: "08:00 AM",
        location: "Coorg → Wayanad",
        activity: "Thamarassery Churam pass & tea factory",
        dwell: "Final destination",
        distance: "90 km (~2.5 hrs)",
      },
    ],
  },
  indepth: {
    id: "indepth",
    label: "In-Depth",
    days: 5,
    nights: 4,
    description: "Comprehensive multi-night stops with forest reserves and artisan trails.",
    daylightGuidance: "Strict daylight driving; includes forest transit pass timing windows.",
    lodgingEstimate: 12500,
    stops: [
      {
        day: 1,
        time: "06:30 AM",
        location: "Bengaluru → Srirangapatna → Mysuru",
        activity: "Heritage island town & art gallery",
        dwell: "200 min dwell",
        distance: "145 km (~2.6 hrs)",
      },
      {
        day: 2,
        time: "08:00 AM",
        location: "Mysuru → Nagarhole National Park",
        activity: "Forest buffer drive & wildlife sanctuary boundary",
        dwell: "Overnight safari lodge",
        distance: "90 km (~2.2 hrs)",
      },
      {
        day: 3,
        time: "09:00 AM",
        location: "Nagarhole → Coorg (Madikeri)",
        activity: "Estate stay check-in & artisan spice trail",
        dwell: "Overnight estate homestay",
        distance: "75 km (~2.0 hrs)",
      },
      {
        day: 4,
        time: "08:30 AM",
        location: "Coorg → Wayanad (Sulthan Bathery)",
        activity: "Edakkal caves & ancient Jain temple ruins",
        dwell: "180 min dwell",
        distance: "105 km (~3.0 hrs)",
      },
      {
        day: 5,
        time: "09:00 AM",
        location: "Wayanad (Vythiri)",
        activity: "Chembra peak ridge view & quiet forest departure",
        dwell: "Wrap-up day",
        distance: "40 km local (~1.2 hrs)",
      },
    ],
  },
};

export function TripExample() {
  const [selectedPace, setSelectedPace] = useState<PaceMode>("balanced");
  const [selectedDay, setSelectedDay] = useState<number>(1);

  const currentFixture = PACE_FIXTURES[selectedPace];
  const activeStop = currentFixture.stops.find((s) => s.day === selectedDay) || currentFixture.stops[0];

  const fuelCost = 2323.0;
  const tollCost = 320.0;
  const lodgingCost = currentFixture.lodgingEstimate;
  const knownTotal = fuelCost + tollCost + lodgingCost;

  return (
    <section
      id="approach"
      className="relative py-24 sm:py-32 px-6 sm:px-12 bg-[#0D1915] text-[#F7F7F2] border-b border-[#233E32]"
      aria-label="Scene 4: The Plan, Made Tangible"
    >
      {/* Anchor alias for backwards compatibility */}
      <span id="how-it-works" className="sr-only" />

      <div className="mx-auto max-w-6xl">
        {/* Section Heading */}
        <div className="max-w-2xl mb-12 sm:mb-16 space-y-3">
          <span className="text-xs font-semibold tracking-widest text-[#B7C9AD] uppercase font-mono">
            How It Works
          </span>
          <h2 className="font-serif text-3xl sm:text-5xl lg:text-6xl font-bold tracking-tight text-[#F7F7F2]">
            See a journey take shape.
          </h2>
          <p className="text-base sm:text-lg text-[#A9B8AD] max-w-xl font-normal leading-relaxed">
            From initial parameters to an itemized, daylight-balanced daily schedule. Test how rhythm and budget update dynamically.
          </p>
        </div>

        {/* Interactive Workspace Card */}
        <div className="rounded-3xl bg-[#15271F] border border-[#233E32] p-6 sm:p-10 shadow-2xl space-y-8">
          {/* Top Bar: Pace Switcher */}
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-6 border-b border-[#233E32]">
            <div>
              <span className="text-xs font-mono text-[#6E8274] uppercase tracking-wider block mb-1">
                Step 1 • Select Journey Pace
              </span>
              <div className="flex items-center gap-2">
                <span className="font-bold text-[#F7F7F2] text-lg">Pacing Preference:</span>
                <span className="text-xs text-[#A9B8AD] font-mono">
                  {currentFixture.days} Days • {currentFixture.nights} Nights
                </span>
              </div>
            </div>

            {/* Segmented Pace Controller */}
            <div
              className="inline-flex rounded-xl bg-[#0D1915] p-1 border border-[#233E32]"
              role="radiogroup"
              aria-label="Select pacing rhythm"
            >
              {(["unhurried", "balanced", "indepth"] as PaceMode[]).map((mode) => {
                const isSelected = selectedPace === mode;
                return (
                  <button
                    key={mode}
                    type="button"
                    role="radio"
                    aria-checked={isSelected}
                    onClick={() => {
                      setSelectedPace(mode);
                      setSelectedDay(1);
                    }}
                    className={`px-4 py-2 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
                      isSelected
                        ? "bg-[#B7C9AD] text-[#102D25] shadow-md"
                        : "text-[#A9B8AD] hover:text-[#F7F7F2]"
                    }`}
                  >
                    {PACE_FIXTURES[mode].label}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Guidance Banner */}
          <div className="flex items-center gap-3 p-4 rounded-xl bg-[#0D1915]/60 border border-[#233E32] text-xs text-[#A9B8AD]">
            <Clock className="h-4 w-4 text-[#B7C9AD] shrink-0" />
            <span>
              <strong className="text-[#F7F7F2]">Daylight Guidance:</strong> {currentFixture.daylightGuidance}
            </span>
          </div>

          {/* Two-Column Display: Schedule vs Budget Ledger */}
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
            {/* Left: Day Stops & Timeline (7 cols) */}
            <div className="lg:col-span-7 space-y-4">
              <div className="flex items-center justify-between">
                <span className="text-xs font-mono text-[#6E8274] uppercase tracking-wider">
                  Step 2 • Daily Schedule Timeline
                </span>
                <span className="text-xs text-[#B7C9AD]">Click a day to inspect</span>
              </div>

              {/* Day Selector Pills */}
              <div className="flex gap-2 pb-1 overflow-x-auto">
                {currentFixture.stops.map((s) => (
                  <button
                    key={s.day}
                    type="button"
                    onClick={() => setSelectedDay(s.day)}
                    className={`px-3.5 py-1.5 rounded-lg text-xs font-medium font-mono transition-all cursor-pointer ${
                      selectedDay === s.day
                        ? "bg-[#1b3329] text-[#B7C9AD] border border-[#B7C9AD]/40"
                        : "bg-[#0D1915] text-[#A9B8AD] border border-[#233E32] hover:text-[#F7F7F2]"
                    }`}
                  >
                    Day {s.day}
                  </button>
                ))}
              </div>

              {/* Detailed Card for Selected Day */}
              <div className="p-5 rounded-2xl bg-[#0D1915] border border-[#233E32] space-y-3">
                <div className="flex items-center justify-between text-xs">
                  <span className="px-2.5 py-0.5 rounded bg-[#15271F] text-[#B7C9AD] font-mono border border-[#B7C9AD]/20">
                    Day {activeStop.day} of {currentFixture.days}
                  </span>
                  <span className="font-mono text-[#6E8274]">{activeStop.distance}</span>
                </div>

                <h3 className="font-bold text-base text-[#F7F7F2]">
                  {activeStop.location}
                </h3>

                <p className="text-xs sm:text-sm text-[#A9B8AD] leading-relaxed">
                  {activeStop.activity}
                </p>

                <div className="pt-2 flex items-center gap-4 text-xs text-[#6E8274] border-t border-[#233E32]/60">
                  <span>Planned Start: <strong className="text-[#F7F7F2] font-mono">{activeStop.time}</strong></span>
                  <span>•</span>
                  <span>{activeStop.dwell}</span>
                </div>
              </div>
            </div>

            {/* Right: Transparent Budget Ledger (5 cols) */}
            <div className="lg:col-span-5 rounded-2xl bg-[#0D1915] border border-[#233E32] p-6 space-y-5">
              <div className="flex items-center justify-between pb-3 border-b border-[#233E32]">
                <div className="flex items-center gap-2">
                  <Sparkles className="h-4 w-4 text-[#C56C4D]" />
                  <h3 className="text-xs font-bold uppercase tracking-wider text-[#F7F7F2] font-mono">
                    Step 3 • Budget Ledger
                  </h3>
                </div>
                <span className="text-[11px] text-[#6E8274] font-mono">Itemized</span>
              </div>

              {/* Itemized Lines */}
              <div className="space-y-3 text-xs">
                <div className="flex justify-between items-center py-1">
                  <div>
                    <span className="text-[#F7F7F2] font-medium block">Estimated Fuel</span>
                    <span className="text-[11px] text-[#6E8274]">340 km @ 15 km/L @ ₹102.5/L</span>
                  </div>
                  <span className="font-mono font-semibold text-[#F7F7F2]">
                    ₹{fuelCost.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
                  </span>
                </div>

                <div className="flex justify-between items-center py-1 border-t border-[#233E32]/40">
                  <div>
                    <span className="text-[#F7F7F2] font-medium block">Highway Tolls</span>
                    <span className="text-[11px] text-[#6E8274]">Fastag Toll Plazas (Verified)</span>
                  </div>
                  <span className="font-mono font-semibold text-[#F7F7F2]">
                    ₹{tollCost.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
                  </span>
                </div>

                <div className="flex justify-between items-center py-1 border-t border-[#233E32]/40">
                  <div>
                    <span className="text-[#F7F7F2] font-medium block">Lodging Estimate</span>
                    <span className="text-[11px] text-[#6E8274]">{currentFixture.nights} Nights (1 Room)</span>
                  </div>
                  <span className="font-mono font-semibold text-[#F7F7F2]">
                    ₹{lodgingCost.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
                  </span>
                </div>

                {/* Explicit Unknown Item (Zero Coercion Standard) */}
                <div className="p-3 rounded-xl bg-amber-500/10 border border-amber-500/20 text-xs text-amber-200 flex items-start gap-2.5">
                  <AlertCircle className="h-4 w-4 text-amber-400 shrink-0 mt-0.5" />
                  <div className="space-y-0.5">
                    <span className="font-bold text-amber-300 block">
                      Ghat Corridor Entry Permit
                    </span>
                    <p className="text-[11px] text-amber-200/80 leading-relaxed">
                      Unknown fee (seasonal permit). Explicitly left uncalculated rather than coerced to ₹0.
                    </p>
                  </div>
                </div>
              </div>

              {/* Subtotal Summary Box */}
              <div className="pt-3 border-t border-[#233E32] space-y-2">
                <div className="flex justify-between items-baseline">
                  <span className="text-xs font-bold text-[#A9B8AD]">Known Subtotal</span>
                  <span className="font-mono font-bold text-lg text-[#B7C9AD]">
                    ₹{knownTotal.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
                  </span>
                </div>
                <p className="text-[11px] text-[#6E8274] leading-relaxed italic">
                  *Illustrative estimates. Your plan compiles deterministically on the server.
                </p>
              </div>

              {/* Action Button */}
              <Link
                href="/dashboard"
                className="w-full inline-flex items-center justify-center gap-2 rounded-xl bg-[#B7C9AD] py-3 text-xs font-semibold text-[#102D25] hover:bg-[#C9DBBE] transition-all cursor-pointer"
              >
                <span>Customize in Planner</span>
                <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

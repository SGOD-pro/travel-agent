"use client";

import React, { useState, useEffect, use } from "react";
import Link from "next/link";
import QRCode from "qrcode";
import { Navbar } from "@/components/layout/Navbar";
import { Footer } from "@/components/layout/Footer";
import CorridorMapWrapper from "@/components/map/CorridorMapWrapper";
import CorridorWeatherAdvisory, { StopWeather } from "@/components/weather/CorridorWeatherAdvisory";
import VoiceBriefAssistant from "@/components/voice/VoiceBriefAssistant";
import {
  Share2,
  QrCode,
  Printer,
  Calendar,
  Clock,
  ExternalLink,
  ChevronRight,
  ShieldCheck,
  Check,
  X,
  Compass,
  MapPin,
  Car,
} from "lucide-react";

interface PublicTripPageProps {
  params: Promise<{ id: string }>;
}

export default function PublicTripPage({ params }: PublicTripPageProps) {
  const resolvedParams = use(params);
  const tripId = resolvedParams.id;

  const [copied, setCopied] = useState(false);
  const [showQrModal, setShowQrModal] = useState(false);
  const [qrCodeDataUrl, setQrCodeDataUrl] = useState<string>("");

  const tripTitle = "Bengaluru to Coorg Heritage & Coffee Trail";
  const tripDuration = "3 Days • 2 Nights";
  const tripDates = "Nov 14 - Nov 16, 2026";

  const stops = [
    {
      name: "Bengaluru (MG Road Origin)",
      lat: 12.9716,
      lng: 77.5946,
      arrivalTime: "06:30 AM",
      departureTime: "07:00 AM",
    },
    {
      name: "Mysuru (Palace & Heritage Zone)",
      lat: 12.2958,
      lng: 76.6394,
      stayMinutes: 240,
      arrivalTime: "10:15 AM",
      departureTime: "02:30 PM",
    },
    {
      name: "Madikeri, Coorg (Estate Homestay)",
      lat: 12.4244,
      lng: 75.7382,
      stayMinutes: 1200,
      arrivalTime: "05:45 PM",
      departureTime: "11:00 AM",
    },
  ];

  const places = [
    {
      name: "Ranganathittu Bird Sanctuary",
      category: "Nature",
      fitScore: 0.94,
      detourKm: 4.2,
      extraDurationMins: 45,
      source: "Karnataka Tourism (KSTDC)",
      uncertainty: "EDITORIAL_DISCOVERY",
      lat: 12.4239,
      lng: 76.6947,
    },
    {
      name: "Dubare Elephant Camp",
      category: "Wildlife",
      fitScore: 0.89,
      detourKm: 6.8,
      extraDurationMins: 90,
      source: "Jungle Lodges & Resorts",
      uncertainty: "EDITORIAL_DISCOVERY",
      lat: 12.3683,
      lng: 75.9038,
    },
    {
      name: "Abbey Falls",
      category: "Scenic",
      fitScore: 0.91,
      detourKm: 7.5,
      extraDurationMins: 60,
      source: "KSTDC Verified",
      uncertainty: "EDITORIAL_DISCOVERY",
      lat: 12.4542,
      lng: 75.7196,
    },
  ];

  const weatherStops: StopWeather[] = [
    {
      stopName: "Bengaluru",
      temperatureC: 24,
      condition: "sunny",
      humidityPercent: 55,
      monsoonRisk: "low",
    },
    {
      stopName: "Mysuru",
      temperatureC: 27,
      condition: "cloudy",
      humidityPercent: 62,
      monsoonRisk: "low",
    },
    {
      stopName: "Madikeri (Coorg)",
      temperatureC: 19,
      condition: "foggy",
      humidityPercent: 88,
      monsoonRisk: "moderate",
      ghatCaution: "Misty mountain passes; maintain low-beam headlights on Sampaje Ghat.",
    },
  ];

  useEffect(() => {
    if (typeof window !== "undefined") {
      const shareUrl = window.location.href;
      QRCode.toDataURL(shareUrl, {
        width: 320,
        margin: 2,
        color: {
          dark: "#102D25",
          light: "#F7F7F2",
        },
      })
        .then((url) => setQrCodeDataUrl(url))
        .catch((err) => console.error("QR Code generation error:", err));
    }
  }, []);

  const handleCopyLink = () => {
    if (typeof window !== "undefined") {
      navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    }
  };

  const handlePrint = () => {
    if (typeof window !== "undefined") {
      window.print();
    }
  };

  const narrativeScript = `Welcome to your itinerary for ${tripTitle}. This 3-day journey begins in Bengaluru at 6:30 AM, driving through the expressway to Mysuru Palace. After exploring the heritage zone and enjoying authentic Mysore Pak, the route ascends the lush Western Ghats to Madikeri, Coorg, arriving by 5:45 PM for a 2-night estate stay. All road tolls along the highway are monitored, and local ghat weather advisories are active.`;

  return (
    <div className="min-h-screen bg-[#0D1915] text-[#F7F7F2] flex flex-col font-sans">
      <Navbar />

      <main className="flex-1 max-w-7xl mx-auto w-full px-4 sm:px-6 lg:px-8 py-28">
        {/* Top Header Card */}
        <div className="bg-[#15271F] border border-[#233E32] rounded-3xl p-6 sm:p-8 shadow-2xl mb-8">
          <div className="flex flex-col md:flex-row md:items-center justify-between gap-6">
            <div>
              <div className="flex items-center gap-2 mb-3">
                <span className="px-3 py-1 rounded-full text-xs font-semibold bg-[#102D25] border border-[#233E32] text-[#B7C9AD] flex items-center gap-1.5">
                  <ShieldCheck className="w-3.5 h-3.5 text-[#B7C9AD]" />
                  Verified Trip Plan • #{tripId.slice(0, 8)}
                </span>
                <span className="px-3 py-1 rounded-full text-xs font-semibold bg-[#15271F] border border-[#233E32] text-[#A9B8AD]">
                  {tripDuration}
                </span>
              </div>
              <h1 className="text-2xl sm:text-3xl lg:text-4xl font-bold tracking-tight text-[#F7F7F2]">
                {tripTitle}
              </h1>
              <p className="text-sm text-[#A9B8AD] mt-2 flex items-center gap-4">
                <span className="flex items-center gap-1.5">
                  <Calendar className="w-4 h-4 text-[#B7C9AD]" /> {tripDates}
                </span>
                <span className="flex items-center gap-1.5">
                  <Car className="w-4 h-4 text-[#B7C9AD]" /> Petrol Car (15 km/L)
                </span>
              </p>
            </div>

            {/* Action Buttons */}
            <div className="flex flex-wrap items-center gap-2.5">
              <VoiceBriefAssistant narrativeText={narrativeScript} />

              <button
                onClick={handleCopyLink}
                className="px-3.5 py-2 rounded-xl text-xs font-semibold bg-[#102D25] text-[#F7F7F2] border border-[#233E32] hover:border-[#B7C9AD] flex items-center gap-1.5 transition-colors cursor-pointer"
              >
                {copied ? <Check className="w-3.5 h-3.5 text-[#B7C9AD]" /> : <Share2 className="w-3.5 h-3.5 text-[#B7C9AD]" />}
                <span>{copied ? "Copied Link!" : "Share Trip"}</span>
              </button>

              <button
                onClick={() => setShowQrModal(true)}
                className="px-3.5 py-2 rounded-xl text-xs font-semibold bg-[#102D25] text-[#F7F7F2] border border-[#233E32] hover:border-[#B7C9AD] flex items-center gap-1.5 transition-colors cursor-pointer"
                title="Scan QR Code to open on mobile"
              >
                <QrCode className="w-3.5 h-3.5 text-[#B7C9AD]" />
                <span>QR Code</span>
              </button>

              <button
                onClick={handlePrint}
                className="btn-sage px-4 py-2 rounded-xl text-xs font-semibold flex items-center gap-1.5 cursor-pointer shadow-md"
              >
                <Printer className="w-3.5 h-3.5" />
                <span>Print / PDF</span>
              </button>
            </div>
          </div>
        </div>

        {/* Geospatial Map Canvas */}
        <div className="mb-8">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-base font-semibold text-[#F7F7F2] flex items-center gap-2">
              <Compass className="w-4 h-4 text-[#B7C9AD]" />
              Interactive Corridor Map & Detour POIs
            </h2>
            <span className="text-xs text-[#A9B8AD]">Dark Matter PostGIS Engine</span>
          </div>
          <div className="h-[420px] w-full rounded-2xl overflow-hidden border border-[#233E32] shadow-2xl">
            <CorridorMapWrapper stops={stops} places={places} />
          </div>
        </div>

        {/* Corridor Weather & Ghat Advisory */}
        <div className="mb-8">
          <CorridorWeatherAdvisory stops={weatherStops} />
        </div>

        {/* Schedule & Budget Columns */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8 mb-12">
          {/* Solved Schedule Column (2 Cols) */}
          <div className="lg:col-span-2 space-y-6">
            <div className="bg-[#15271F] rounded-2xl border border-[#233E32] p-6 shadow-xl">
              <h3 className="text-lg font-bold text-[#F7F7F2] mb-6 flex items-center gap-2">
                <Clock className="w-5 h-5 text-[#B7C9AD]" />
                Solved Itinerary Schedule
              </h3>

              <div className="space-y-6 relative before:absolute before:left-3.5 before:top-3 before:bottom-3 before:w-0.5 before:bg-[#233E32]">
                {/* Stop 1 */}
                <div className="relative pl-9">
                  <div className="absolute left-1.5 top-1.5 w-4 h-4 rounded-full bg-[#B7C9AD] border-4 border-[#15271F]" />
                  <div className="bg-[#0D1915] p-4 rounded-xl border border-[#233E32]">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold text-[#B7C9AD]">Day 1 • 06:30 AM</span>
                      <span className="text-xs text-[#A9B8AD]">Origin Departure</span>
                    </div>
                    <h4 className="text-base font-bold text-[#F7F7F2] mt-1">Bengaluru (MG Road)</h4>
                    <p className="text-xs text-[#A9B8AD] mt-1">
                      Depart via Bengaluru-Mysuru Expressway (NH 275). Early departure minimizes city exit traffic.
                    </p>
                  </div>
                </div>

                {/* Stop 2 */}
                <div className="relative pl-9">
                  <div className="absolute left-1.5 top-1.5 w-4 h-4 rounded-full bg-[#15271F] border-2 border-[#B7C9AD]" />
                  <div className="bg-[#0D1915] p-4 rounded-xl border border-[#233E32]">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold text-[#B7C9AD]">Day 1 • 10:15 AM - 02:30 PM</span>
                      <span className="text-xs text-[#A9B8AD]">4 Hours Stay</span>
                    </div>
                    <h4 className="text-base font-bold text-[#F7F7F2] mt-1">Mysuru Heritage Zone</h4>
                    <p className="text-xs text-[#A9B8AD] mt-1">
                      Guided visit through Amba Vilas Palace and Devaraja Market. Lunch at Mylari Dosa.
                    </p>
                    <div className="mt-3 flex items-center gap-2">
                      <a
                        href="https://kstdc.co/"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-xs text-[#B7C9AD] hover:underline"
                      >
                        Search on KSTDC <ExternalLink className="w-3 h-3" />
                      </a>
                    </div>
                  </div>
                </div>

                {/* Stop 3 */}
                <div className="relative pl-9">
                  <div className="absolute left-1.5 top-1.5 w-4 h-4 rounded-full bg-[#B7C9AD] border-4 border-[#15271F]" />
                  <div className="bg-[#0D1915] p-4 rounded-xl border border-[#233E32]">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold text-[#B7C9AD]">Day 1 - 3 • 05:45 PM Arrival</span>
                      <span className="text-xs text-[#A9B8AD]">2 Nights Stay</span>
                    </div>
                    <h4 className="text-base font-bold text-[#F7F7F2] mt-1">Madikeri, Coorg</h4>
                    <p className="text-xs text-[#A9B8AD] mt-1">
                      Check in to coffee estate homestay. Includes visits to Raja&apos;s Seat, Abbey Falls, and Dubare Sanctuary.
                    </p>
                    <div className="mt-3 flex items-center gap-2">
                      <a
                        href="https://www.irctctourism.com/"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-xs text-[#B7C9AD] hover:underline"
                      >
                        View on IRCTC Tourism <ExternalLink className="w-3 h-3" />
                      </a>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Itemized Budget Breakdown (1 Col) */}
          <div className="space-y-6">
            <div className="bg-[#15271F] rounded-2xl border border-[#233E32] p-6 shadow-xl">
              <h3 className="text-lg font-bold text-[#F7F7F2] mb-4">Itemized Budget</h3>
              <p className="text-xs text-[#A9B8AD] mb-6">
                Calculated for 2 travelers sharing 1 vehicle.
              </p>

              <div className="space-y-3.5 border-b border-[#233E32] pb-6 mb-6">
                <div className="flex justify-between items-center text-sm">
                  <span className="text-[#A9B8AD]">Vehicle Fuel (520 km @ ₹102.5/L)</span>
                  <span className="font-semibold text-[#F7F7F2]">₹3,553.33</span>
                </div>

                <div className="flex justify-between items-center text-sm">
                  <span className="text-[#A9B8AD]">Accommodation (2 Nights)</span>
                  <span className="font-semibold text-[#F7F7F2]">₹7,000.00</span>
                </div>

                <div className="flex justify-between items-center text-sm">
                  <span className="text-[#A9B8AD]">Activities & Entry Permits</span>
                  <span className="font-semibold text-[#F7F7F2]">₹1,200.00</span>
                </div>

                {/* Invariant: zero-coercion rule */}
                <div className="flex justify-between items-center text-sm p-2 rounded-lg bg-[#0D1915] border border-amber-500/20">
                  <span className="text-amber-400/90 text-xs">Expressway Tolls</span>
                  <span className="text-amber-400 font-semibold text-xs uppercase tracking-wider">
                    Unknown
                  </span>
                </div>
              </div>

              {/* Explicit zero-coercion label */}
              <div>
                <div className="text-[11px] uppercase tracking-wider text-[#A9B8AD] font-medium">
                  Known/estimated subtotal; tolls unknown
                </div>
                <div className="text-3xl font-bold text-[#B7C9AD] mt-1">₹11,753.33</div>
                <div className="text-xs text-[#A9B8AD] mt-1">
                  ₹5,876.67 per person (2 travelers)
                </div>
              </div>

              <div className="mt-6 pt-4 border-t border-[#233E32]/60 text-[11px] text-[#A9B8AD] leading-relaxed">
                * Zero-coercion guarantee: Unquoted highway tolls are preserved as unknown and not coerced to ₹0.
              </div>
            </div>
          </div>
        </div>
      </main>

      {/* QR Code Modal for Instant Mobile Loading */}
      {showQrModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4">
          <div className="bg-[#15271F] border border-[#233E32] rounded-3xl p-6 sm:p-8 max-w-sm w-full text-center relative shadow-2xl">
            <button
              onClick={() => setShowQrModal(false)}
              className="absolute top-4 right-4 p-2 rounded-full text-[#A9B8AD] hover:text-[#F7F7F2] hover:bg-[#0D1915]"
            >
              <X className="w-5 h-5" />
            </button>

            <h3 className="text-lg font-bold text-[#F7F7F2] mb-1">Open Trip on Mobile</h3>
            <p className="text-xs text-[#A9B8AD] mb-6">
              Scan with your phone&apos;s camera to follow this live itinerary on the road.
            </p>

            {qrCodeDataUrl ? (
              <div className="bg-[#F7F7F2] p-4 rounded-2xl inline-block mx-auto shadow-inner">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={qrCodeDataUrl}
                  alt="Trip Share QR Code"
                  className="w-56 h-56 mx-auto rounded-lg"
                />
              </div>
            ) : (
              <div className="w-56 h-56 bg-[#0D1915] rounded-2xl flex items-center justify-center mx-auto text-[#A9B8AD] text-xs">
                Generating QR Code...
              </div>
            )}

            <div className="mt-6">
              <button
                onClick={() => setShowQrModal(false)}
                className="btn-sage w-full py-2.5 rounded-xl text-xs font-semibold cursor-pointer"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      <Footer />
    </div>
  );
}

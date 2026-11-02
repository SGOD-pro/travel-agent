"use client";

import React, { useState, useEffect, use, useCallback } from "react";
import Link from "next/link";
import QRCode from "qrcode";
import { Navbar } from "@/components/layout/Navbar";
import { Footer } from "@/components/layout/Footer";
import CorridorMapWrapper from "@/components/map/CorridorMapWrapper";
import CorridorWeatherAdvisory, { StopWeather } from "@/components/weather/CorridorWeatherAdvisory";
import VoiceBriefAssistant from "@/components/voice/VoiceBriefAssistant";
import type {
  ItineraryResponse,
  TripResponse,
  ScheduleStop,
  EditorialRecommendation,
  BudgetLine,
} from "@/types/itinerary";
import type { MapStop, MapPlace } from "@/components/map/CorridorMap";
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
  AlertTriangle,
  RefreshCw,
  Lock,
  Search,
  Sparkles,
  Info,
  DollarSign,
  FileText,
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

  // Data states
  const [itinerary, setItinerary] = useState<ItineraryResponse | null>(null);
  const [trip, setTrip] = useState<TripResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [statusCode, setStatusCode] = useState<number | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

  const fetchTripData = useCallback(async () => {
    setLoading(true);
    setErrorMessage(null);
    setStatusCode(null);

    try {
      // 1. Fetch itinerary proposal
      const itinRes = await fetch(`${apiBase}/api/v1/trips/${tripId}/itinerary`, {
        headers: { "Content-Type": "application/json" },
      });

      if (!itinRes.ok) {
        setStatusCode(itinRes.status);
        const errJson = await itinRes.json().catch(() => ({}));
        const detail = errJson.detail || `Request failed with HTTP ${itinRes.status}`;
        setErrorMessage(detail);
        setLoading(false);
        return;
      }

      const itinData: ItineraryResponse = await itinRes.json();
      setItinerary(itinData);
      setStatusCode(200);

      // 2. Fetch basic trip metadata for origin/destination names and brief dates
      try {
        const tripRes = await fetch(`${apiBase}/api/v1/trips/${tripId}`, {
          headers: { "Content-Type": "application/json" },
        });
        if (tripRes.ok) {
          const tripData: TripResponse = await tripRes.json();
          setTrip(tripData);
        }
      } catch (err) {
        console.warn("Could not fetch supplementary trip metadata:", err);
      }
    } catch (err: any) {
      console.error("Failed to load itinerary proposal:", err);
      setErrorMessage(err.message || "Network error loading itinerary proposal");
      setStatusCode(0);
    } finally {
      setLoading(false);
    }
  }, [apiBase, tripId]);

  useEffect(() => {
    fetchTripData();
  }, [fetchTripData]);

  // QR Code generation
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

  // Derive title, dates, duration from trip brief or schedule
  const brief = trip?.brief;
  const tripTitle = brief
    ? `${brief.origin_name} to ${brief.destinations.map((d) => d.name).join(", ")}`
    : itinerary?.schedule?.stops && itinerary.schedule.stops.length > 1
    ? `${itinerary.schedule.stops[0].name} to ${itinerary.schedule.stops[itinerary.schedule.stops.length - 1].name}`
    : `Trip Itinerary Proposal #${tripId.slice(0, 8)}`;

  const tripDates = brief?.start_date && brief?.end_date
    ? `${brief.start_date} to ${brief.end_date}`
    : "Travel Window Verified";

  const totalDays = brief?.start_date && brief?.end_date
    ? Math.max(
        1,
        Math.round(
          (new Date(brief.end_date).getTime() - new Date(brief.start_date).getTime()) /
            (1000 * 60 * 60 * 24)
        ) + 1
      )
    : itinerary?.schedule?.stops ? Math.max(1, Math.ceil(itinerary.schedule.stops.length / 3)) : 1;

  const tripDuration = `${totalDays} Day${totalDays > 1 ? "s" : ""} • ${Math.max(1, totalDays - 1)} Night${totalDays > 2 ? "s" : ""}`;

  // Map schedule stops to MapStop format
  const stops: MapStop[] = (itinerary?.schedule?.stops || []).map((s) => ({
    name: s.name,
    lat: s.latitude,
    lng: s.longitude,
    stayMinutes: s.stay_duration_minutes,
    arrivalTime: s.arrival_time ? new Date(s.arrival_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : undefined,
    departureTime: s.departure_time ? new Date(s.departure_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : undefined,
  }));

  // Map editorial recommendations to MapPlace format
  const places: MapPlace[] = (itinerary?.editorial_recommendations || []).map((r) => ({
    name: r.name || r.primary_name || "Discovered Place",
    category: r.category || "Discovery",
    fitScore: r.fit_score ?? 0.85,
    detourKm: r.detour_km ?? 5.0,
    extraDurationMins: 45,
    source: r.source || "AI Discovery Gate",
    uncertainty: r.uncertainty || "EDITORIAL_DISCOVERY",
    lat: r.latitude ?? r.lat ?? 0,
    lng: r.longitude ?? r.lng ?? 0,
    description: r.description,
  }));

  // Map weather advisories to StopWeather format
  const weatherStops: StopWeather[] = (itinerary?.weather_advisories || []).map((w) => ({
    stopName: w.stop_name || w.stopName || "Corridor Stop",
    temperatureC: Math.round(w.temperature_celsius ?? w.temperatureC ?? 24),
    condition: (["sunny", "cloudy", "rainy", "foggy"].includes(w.condition as any)
      ? w.condition
      : "cloudy") as StopWeather["condition"],
    humidityPercent: w.humidity_percent ?? w.humidityPercent ?? 55,
    monsoonRisk: (["low", "moderate", "severe"].includes(w.monsoon_risk as any)
      ? w.monsoon_risk
      : (w.monsoonRisk as any) || "low") as StopWeather["monsoonRisk"],
    ghatCaution: w.ghat_caution || w.ghatCaution || undefined,
  }));

  // Prepare narrative for voice assistant
  const narrativeScript = itinerary?.schedule?.stops && itinerary.schedule.stops.length > 0
    ? `Welcome to your itinerary for ${tripTitle}. This journey visits ${itinerary.schedule.stops.map((s) => s.name).join(", ")}. Operating hours and travel corridors have been verified by the SWENA planner.`
    : `Welcome to your travel proposal for ${tripTitle}.`;

  return (
    <div className="min-h-screen bg-[#0D1915] text-[#F7F7F2] flex flex-col font-sans">
      <Navbar />

      <main className="flex-1 max-w-7xl mx-auto w-full px-4 sm:px-6 lg:px-8 py-28">
        {/* State 1: Loading Skeleton */}
        {loading && (
          <div className="bg-[#15271F] border border-[#233E32] rounded-3xl p-8 sm:p-12 shadow-2xl flex flex-col items-center justify-center text-center my-12 min-h-96">
            <div className="w-12 h-12 rounded-full border-3 border-[#B7C9AD] border-t-transparent animate-spin mb-4" />
            <h2 className="text-xl font-bold text-[#F7F7F2] mb-2">
              Loading Itinerary Proposal
            </h2>
            <p className="text-sm text-[#A9B8AD] max-w-md">
              Connecting to SWENA trip planner to retrieve verified stops, route solver state, and corridor weather advisories...
            </p>
          </div>
        )}

        {/* State 2: Planning in Progress (HTTP 409 Conflict) */}
        {!loading && statusCode === 409 && (
          <div className="bg-[#15271F] border border-amber-500/30 rounded-3xl p-8 sm:p-12 shadow-2xl text-center my-12">
            <div className="w-14 h-14 rounded-2xl bg-amber-500/10 border border-amber-500/20 text-amber-400 flex items-center justify-center mx-auto mb-5">
              <RefreshCw className="w-7 h-7 animate-spin text-amber-400" />
            </div>
            <h2 className="text-2xl font-bold text-[#F7F7F2] mb-3">
              Trip Planning in Progress
            </h2>
            <p className="text-sm text-[#A9B8AD] max-w-lg mx-auto mb-6 leading-relaxed">
              The AI planner and mathematical route optimizer are currently researching candidate places, checking spatial corridors, gathering weather advisories, and solving schedules for this trip.
            </p>
            <div className="flex flex-wrap items-center justify-center gap-3">
              <button
                onClick={fetchTripData}
                className="btn-sage px-5 py-2.5 rounded-xl text-xs font-semibold flex items-center gap-2 cursor-pointer shadow-md"
              >
                <RefreshCw className="w-4 h-4" />
                <span>Check Status Now</span>
              </button>
              <Link
                href="/dashboard"
                className="px-5 py-2.5 rounded-xl text-xs font-semibold bg-[#102D25] text-[#A9B8AD] hover:text-[#F7F7F2] border border-[#233E32] hover:border-[#B7C9AD] transition-colors"
              >
                Return to Dashboard
              </Link>
            </div>
          </div>
        )}

        {/* State 3: Access Forbidden (HTTP 403 / 401) */}
        {!loading && (statusCode === 403 || statusCode === 401) && (
          <div className="bg-[#15271F] border border-red-500/30 rounded-3xl p-8 sm:p-12 shadow-2xl text-center my-12">
            <div className="w-14 h-14 rounded-2xl bg-red-500/10 border border-red-500/20 text-red-400 flex items-center justify-center mx-auto mb-5">
              <Lock className="w-7 h-7 text-red-400" />
            </div>
            <h2 className="text-2xl font-bold text-[#F7F7F2] mb-3">
              Access Forbidden
            </h2>
            <p className="text-sm text-[#A9B8AD] max-w-lg mx-auto mb-6 leading-relaxed">
              Strict tenant isolation protects this travel proposal. This trip belongs to another traveler account and cannot be viewed without authorized credentials.
            </p>
            <Link
              href="/dashboard"
              className="btn-sage px-5 py-2.5 rounded-xl text-xs font-semibold inline-flex items-center gap-2"
            >
              Back to My Dashboard
            </Link>
          </div>
        )}

        {/* State 4: Not Found (HTTP 404) */}
        {!loading && statusCode === 404 && (
          <div className="bg-[#15271F] border border-[#233E32] rounded-3xl p-8 sm:p-12 shadow-2xl text-center my-12">
            <div className="w-14 h-14 rounded-2xl bg-[#102D25] border border-[#233E32] text-[#A9B8AD] flex items-center justify-center mx-auto mb-5">
              <Search className="w-7 h-7 text-[#B7C9AD]" />
            </div>
            <h2 className="text-2xl font-bold text-[#F7F7F2] mb-3">
              Itinerary Proposal Not Found
            </h2>
            <p className="text-sm text-[#A9B8AD] max-w-lg mx-auto mb-6 leading-relaxed">
              No proposal has been published for trip #{tripId.slice(0, 8)}. If this trip was recently submitted, planning may not have started yet.
            </p>
            <div className="flex flex-wrap items-center justify-center gap-3">
              <Link
                href="/dashboard"
                className="btn-sage px-5 py-2.5 rounded-xl text-xs font-semibold inline-flex items-center gap-2"
              >
                Go to Dashboard
              </Link>
              <button
                onClick={fetchTripData}
                className="px-5 py-2.5 rounded-xl text-xs font-semibold bg-[#102D25] text-[#A9B8AD] hover:text-[#F7F7F2] border border-[#233E32] transition-colors cursor-pointer"
              >
                Retry
              </button>
            </div>
          </div>
        )}

        {/* State 5: Generic Error Banner */}
        {!loading && statusCode !== 200 && statusCode !== 409 && statusCode !== 403 && statusCode !== 401 && statusCode !== 404 && (
          <div className="bg-[#15271F] border border-red-500/30 rounded-3xl p-8 shadow-2xl text-center my-12">
            <div className="w-12 h-12 rounded-2xl bg-red-500/10 border border-red-500/20 text-red-400 flex items-center justify-center mx-auto mb-4">
              <AlertTriangle className="w-6 h-6 text-red-400" />
            </div>
            <h2 className="text-lg font-bold text-[#F7F7F2] mb-2">
              Failed to Load Itinerary
            </h2>
            <p className="text-xs text-[#A9B8AD] mb-6">
              {errorMessage || "An unexpected error occurred while contacting the itinerary service."}
            </p>
            <button
              onClick={fetchTripData}
              className="btn-sage px-4 py-2 rounded-xl text-xs font-semibold inline-flex items-center gap-2 cursor-pointer"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              <span>Retry Request</span>
            </button>
          </div>
        )}

        {/* State 6: Loaded Proposal View */}
        {!loading && statusCode === 200 && itinerary && (
          <>
            {/* Diagnostic Alert: Routing Unavailable */}
            {(itinerary.routing_status === "ROUTING_UNAVAILABLE" || !itinerary.route_id) && (
              <div className="mb-6 bg-amber-500/10 border border-amber-500/30 rounded-2xl p-4 sm:p-5 flex items-start gap-3.5 shadow-lg">
                <AlertTriangle className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
                <div>
                  <h3 className="text-sm font-bold text-amber-300">
                    Road Routing Engine Unavailable
                  </h3>
                  <p className="text-xs text-amber-200/80 mt-1 leading-relaxed">
                    Road routing engine unavailable; stops preserved without driving schedule or route polyline. In conformance with our fail-closed routing guarantee, straight-line distance guesses or fabricated travel times are never generated.
                  </p>
                  {itinerary.route_diagnostics && itinerary.route_diagnostics.length > 0 && (
                    <ul className="mt-2 space-y-1 text-xs text-amber-300/90 list-disc list-inside">
                      {itinerary.route_diagnostics.map((diag, idx) => (
                        <li key={idx}>{diag}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            )}

            {/* Diagnostic Alert: Route Infeasible */}
            {itinerary.route_status === "INFEASIBLE" && (
              <div className="mb-6 bg-red-500/10 border border-red-500/30 rounded-2xl p-4 sm:p-5 flex items-start gap-3.5 shadow-lg">
                <AlertTriangle className="w-5 h-5 text-red-400 shrink-0 mt-0.5" />
                <div>
                  <h3 className="text-sm font-bold text-red-300">
                    Route Infeasible Diagnostics
                  </h3>
                  <p className="text-xs text-red-200/80 mt-1 leading-relaxed">
                    Overconstrained stop time windows or impossible transit constraints prevented a complete itinerary from solving. Certain stops could not be visited within their operating hours.
                  </p>
                  {itinerary.route_diagnostics && itinerary.route_diagnostics.length > 0 && (
                    <ul className="mt-2 space-y-1 text-xs text-red-300/90 list-disc list-inside">
                      {itinerary.route_diagnostics.map((diag, idx) => (
                        <li key={idx}>{diag}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            )}

            {/* Diagnostic Alert: Distant Weather Forecast Horizon */}
            {itinerary.weather_status === "OUT_OF_FORECAST_HORIZON" && (
              <div className="mb-6 bg-blue-500/10 border border-blue-500/30 rounded-2xl p-4 sm:p-5 flex items-start gap-3.5 shadow-lg">
                <Info className="w-5 h-5 text-blue-400 shrink-0 mt-0.5" />
                <div>
                  <h3 className="text-sm font-bold text-blue-300">
                    Departure Beyond Forecast Horizon
                  </h3>
                  <p className="text-xs text-blue-200/80 mt-1 leading-relaxed">
                    Your scheduled departure date is beyond the 16-day live meteorological forecast model. Regional climate advisories are active; live weather metrics will update automatically closer to your travel window.
                  </p>
                </div>
              </div>
            )}

            {/* Top Header Card */}
            <div className="bg-[#15271F] border border-[#233E32] rounded-3xl p-6 sm:p-8 shadow-2xl mb-8">
              <div className="flex flex-col md:flex-row md:items-center justify-between gap-6">
                <div>
                  <div className="flex items-center gap-2 mb-3">
                    <span className="px-3 py-1 rounded-full text-xs font-semibold bg-[#102D25] border border-[#233E32] text-[#B7C9AD] flex items-center gap-1.5">
                      <ShieldCheck className="w-3.5 h-3.5 text-[#B7C9AD]" />
                      Proposal v{itinerary.version} • #{tripId.slice(0, 8)}
                    </span>
                    <span className="px-3 py-1 rounded-full text-xs font-semibold bg-[#15271F] border border-[#233E32] text-[#A9B8AD]">
                      {tripDuration}
                    </span>
                    <span className="px-3 py-1 rounded-full text-xs font-semibold bg-[#102D25] border border-emerald-500/30 text-emerald-400 uppercase tracking-wider">
                      {itinerary.status || "Ready"}
                    </span>
                  </div>
                  <h1 className="text-2xl sm:text-3xl lg:text-4xl font-bold tracking-tight text-[#F7F7F2]">
                    {tripTitle}
                  </h1>
                  <p className="text-sm text-[#A9B8AD] mt-2 flex flex-wrap items-center gap-4">
                    <span className="flex items-center gap-1.5">
                      <Calendar className="w-4 h-4 text-[#B7C9AD]" /> {tripDates}
                    </span>
                    <span className="flex items-center gap-1.5">
                      <Car className="w-4 h-4 text-[#B7C9AD]" />
                      {brief?.preferred_modes?.[0] ? brief.preferred_modes[0].replace("_", " ").toUpperCase() : "Petrol Car (15 km/L)"}
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
                <div className="flex items-center gap-3">
                  {itinerary.route_id && (
                    <Link
                      href={`/trips/${tripId}/route?route_id=${itinerary.route_id}`}
                      className="text-xs font-semibold text-[#B7C9AD] hover:text-[#F7F7F2] bg-[#102D25] border border-[#233E32] hover:border-[#B7C9AD] px-3 py-1.5 rounded-lg flex items-center gap-1.5 transition-colors"
                    >
                      <span>Interactive Route & Replan</span>
                      <ChevronRight className="w-3.5 h-3.5" />
                    </Link>
                  )}
                  <span className="text-xs text-[#A9B8AD] hidden sm:inline">Dark Matter PostGIS Engine</span>
                </div>
              </div>
              <div className="h-105 w-full rounded-2xl overflow-hidden border border-[#233E32] shadow-2xl">
                <CorridorMapWrapper
                  geoJson={itinerary.routing_status === "ROUTING_UNAVAILABLE" ? null : itinerary.map_geojson}
                  stops={stops}
                  places={places}
                />
              </div>
            </div>

            {/* Corridor Weather & Ghat Advisory */}
            <div className="mb-8">
              <CorridorWeatherAdvisory stops={weatherStops} />
              <div className="mt-2 text-right">
                <span className="text-[11px] text-[#A9B8AD]/70">
                  Weather data by Open Meteo.com
                </span>
              </div>
            </div>

            {/* Schedule & Budget Columns */}
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-8 mb-12">
              {/* Solved Schedule Column (2 Cols) */}
              <div className="lg:col-span-2 space-y-6">
                <div className="bg-[#15271F] rounded-2xl border border-[#233E32] p-6 shadow-xl">
                  <div className="flex items-center justify-between mb-6">
                    <h3 className="text-lg font-bold text-[#F7F7F2] flex items-center gap-2">
                      <Clock className="w-5 h-5 text-[#B7C9AD]" />
                      Solved Itinerary Schedule
                    </h3>
                    {itinerary.schedule && (
                      <span className="text-xs text-[#A9B8AD] bg-[#102D25] px-3 py-1 rounded-full border border-[#233E32]">
                        {itinerary.schedule.stops.length} Stops • {itinerary.schedule.total_travel_minutes} mins travel
                      </span>
                    )}
                  </div>

                  {(!itinerary.schedule?.stops || itinerary.schedule.stops.length === 0) ? (
                    <div className="text-sm text-[#A9B8AD] p-6 text-center">
                      No schedule stops recorded in this proposal.
                    </div>
                  ) : (
                    <div className="space-y-6 relative before:absolute before:left-3.5 before:top-3 before:bottom-3 before:w-0.5 before:bg-[#233E32]">
                      {itinerary.schedule.stops.map((stop: ScheduleStop, idx: number) => {
                        const isOrigin = idx === 0;
                        const isLast = idx === itinerary.schedule!.stops.length - 1;
                        const arrivalStr = stop.arrival_time
                          ? new Date(stop.arrival_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                          : "TBD";
                        const depStr = stop.departure_time
                          ? new Date(stop.departure_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                          : "TBD";

                        return (
                          <div key={idx} className="relative pl-9">
                            <div
                              className={`absolute left-1.5 top-1.5 w-4 h-4 rounded-full border-4 border-[#15271F] ${
                                isOrigin || isLast ? "bg-[#B7C9AD]" : "bg-[#233E32] border-2 border-[#B7C9AD]"
                              }`}
                            />
                            <div className="bg-[#0D1915] p-4 rounded-xl border border-[#233E32]">
                              <div className="flex flex-wrap items-center justify-between gap-2">
                                <span className="text-xs font-semibold text-[#B7C9AD]">
                                  Stop {stop.position} • {arrivalStr} {stop.stay_duration_minutes > 0 ? `- ${depStr}` : ""}
                                </span>
                                <div className="flex items-center gap-2">
                                  {/* AC-3: HOURS_ASSUMED label */}
                                  {stop.is_hours_assumed || stop.hours_status === "HOURS_ASSUMED" ? (
                                    <span
                                      className="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30"
                                      title="Assumed daytime visiting window (09:00 - 18:00); operating hours unverified by source"
                                    >
                                      HOURS_ASSUMED
                                    </span>
                                  ) : stop.hours_status === "CONFIRMED" ? (
                                    <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">
                                      VERIFIED HOURS
                                    </span>
                                  ) : null}

                                  {stop.stay_duration_minutes > 0 && (
                                    <span className="text-xs text-[#A9B8AD]">
                                      {Math.round(stop.stay_duration_minutes / 60)} hrs stay
                                    </span>
                                  )}
                                </div>
                              </div>

                              <h4 className="text-base font-bold text-[#F7F7F2] mt-1.5 flex items-center gap-2">
                                <MapPin className="w-4 h-4 text-[#B7C9AD]" />
                                {stop.name}
                              </h4>

                              {stop.is_hours_assumed && (
                                <p className="text-xs text-amber-200/80 mt-1">
                                  * Daylight visiting hours assumed (09:00 - 18:00). Actual operating times subject to verification.
                                </p>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>

                {/* Candidate Provenance & Editorial Discovery */}
                {itinerary.editorial_recommendations && itinerary.editorial_recommendations.length > 0 && (
                  <div className="bg-[#15271F] rounded-2xl border border-[#233E32] p-6 shadow-xl">
                    <h3 className="text-lg font-bold text-[#F7F7F2] mb-2 flex items-center gap-2">
                      <Sparkles className="w-5 h-5 text-[#B7C9AD]" />
                      Editorial Place Recommendations
                    </h3>
                    <p className="text-xs text-[#A9B8AD] mb-6">
                      Places discovered along your travel corridor. In accordance with AC-3, unverified proposals remain editorial suggestions and never enter the mathematical route solver as confirmed stops.
                    </p>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      {itinerary.editorial_recommendations.map((rec: EditorialRecommendation, idx: number) => {
                        const name = rec.name || rec.primary_name || "Point of Interest";
                        return (
                          <div
                            key={idx}
                            className="bg-[#0D1915] p-4 rounded-xl border border-[#233E32] flex flex-col justify-between"
                          >
                            <div>
                              <div className="flex items-center justify-between mb-1.5">
                                <span className="text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider">
                                  {rec.category || "Discovery"}
                                </span>
                                <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-[#15271F] text-[#A9B8AD] border border-[#233E32]">
                                  {rec.uncertainty || "EDITORIAL_DISCOVERY"}
                                </span>
                              </div>
                              <h4 className="text-sm font-bold text-[#F7F7F2] mb-1">{name}</h4>
                              {rec.description && (
                                <p className="text-xs text-[#A9B8AD] leading-relaxed line-clamp-2">
                                  {rec.description}
                                </p>
                              )}
                            </div>

                            {rec.source && (
                              <div className="mt-3 pt-2 border-t border-[#233E32]/60 text-[11px] text-[#A9B8AD] flex items-center gap-1.5">
                                <Info className="w-3 h-3 text-[#B7C9AD]" />
                                <span className="truncate">Source: {rec.source}</span>
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>

              {/* Itemized Budget Breakdown (1 Col) */}
              <div className="space-y-6">
                <div className="bg-[#15271F] rounded-2xl border border-[#233E32] p-6 shadow-xl">
                  <h3 className="text-lg font-bold text-[#F7F7F2] mb-1 flex items-center gap-2">
                    <DollarSign className="w-5 h-5 text-[#B7C9AD]" />
                    Itemized Budget
                  </h3>
                  <p className="text-xs text-[#A9B8AD] mb-6">
                    Evidence-backed cost breakdown with explicit uncertainty tracking.
                  </p>

                  <div className="space-y-3.5 border-b border-[#233E32] pb-6 mb-6">
                    {itinerary.budget_lines.map((line: BudgetLine, idx: number) => {
                      const isUnknown = line.amount === null;
                      const formattedBasis = line.basis
                        ? line.basis.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
                        : line.category.toUpperCase();

                      if (isUnknown) {
                        return (
                          <div
                            key={idx}
                            className="p-3 rounded-xl bg-[#0D1915] border border-amber-500/20"
                          >
                            <div className="flex justify-between items-center text-sm">
                              <span className="text-amber-300 text-xs font-medium">
                                {formattedBasis}
                              </span>
                              <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-400 border border-amber-500/30 uppercase tracking-wider">
                                UNKNOWN
                              </span>
                            </div>
                            {line.unknown_reason && (
                              <p className="text-[11px] text-amber-200/70 mt-1">
                                {line.unknown_reason}
                              </p>
                            )}
                          </div>
                        );
                      }

                      const amtNum = Number(line.amount);
                      const amtStr = !isNaN(amtNum)
                        ? `₹${amtNum.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                        : `${line.currency} ${line.amount}`;

                      return (
                        <div key={idx} className="flex justify-between items-start text-sm">
                          <div>
                            <span className="text-[#A9B8AD]">{formattedBasis}</span>
                            <div className="flex items-center gap-1.5 mt-0.5">
                              <span
                                className={`text-[10px] font-semibold px-1.5 py-0.2 rounded ${
                                  line.estimated
                                    ? "bg-[#102D25] text-[#A9B8AD] border border-[#233E32]"
                                    : "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                                }`}
                              >
                                {line.estimated ? "ESTIMATED" : "QUOTED"}
                              </span>
                            </div>
                          </div>
                          <span className="font-semibold text-[#F7F7F2]">{amtStr}</span>
                        </div>
                      );
                    })}
                  </div>

                  {/* Subtotal & Zero-Coercion Guarantee */}
                  <div>
                    <div className="text-[11px] uppercase tracking-wider text-[#A9B8AD] font-medium">
                      {itinerary.budget_summary?.is_complete
                        ? "Total Itinerary Budget"
                        : "Known Subtotal (Excluding Unknowns)"}
                    </div>
                    <div className="text-3xl font-bold text-[#B7C9AD] mt-1">
                      {itinerary.budget_summary?.known_total?.amount
                        ? `₹${Number(itinerary.budget_summary.known_total.amount).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                        : "₹0.00"}
                    </div>
                    {!itinerary.budget_summary?.is_complete && (
                      <div className="text-xs text-amber-400/90 mt-1">
                        {itinerary.budget_summary?.unknown_count || 1} cost item(s) unverified
                      </div>
                    )}
                  </div>

                  <div className="mt-6 pt-4 border-t border-[#233E32]/60 text-[11px] text-[#A9B8AD] leading-relaxed">
                    * Zero-coercion guarantee: In conformance with AC-7, unquoted highway tolls and unverified entrance fees remain explicitly unknown and are never coerced to ₹0.
                  </div>
                </div>
              </div>
            </div>
          </>
        )}
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

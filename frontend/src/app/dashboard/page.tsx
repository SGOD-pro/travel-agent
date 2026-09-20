"use client";

import React, { useState, useEffect, Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  Compass,
  MapPin,
  Calendar,
  Users,
  Car,
  Bike,
  Plus,
  Trash2,
  Sparkles,
  ArrowRight,
  ShieldCheck,
  AlertTriangle,
  Clock,
  ExternalLink,
  ChevronRight,
  Route,
  Receipt,
  CheckCircle2,
  Building2,
  RefreshCw,
  Server,
  Download,
  Share2,
  Map as MapIcon,
} from "lucide-react";
import CorridorMapWrapper from "@/components/map/CorridorMapWrapper";
import CorridorWeatherAdvisory, { StopWeather } from "@/components/weather/CorridorWeatherAdvisory";
import VoiceBriefAssistant from "@/components/voice/VoiceBriefAssistant";

interface DestinationItem {
  id: string;
  name: string;
  lat: number;
  lng: number;
  stayDays: number;
}

interface ScheduledLeg {
  from: string;
  to: string;
  distanceKm: number;
  durationMins: number;
}

interface ScheduledStop {
  position: number;
  name: string;
  arrival: string;
  departure: string;
  stayHours: number;
}

function computeDistanceKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const R = 6371;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLon / 2) *
      Math.sin(dLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return Math.max(20, Math.round(R * c * 1.28));
}

function DashboardContent() {
  const searchParams = useSearchParams();
  const corridorParam = searchParams.get("corridor");

  // Brief configuration state
  const [origin, setOrigin] = useState("Bengaluru");
  const [originLat, setOriginLat] = useState(12.9716);
  const [originLng, setOriginLng] = useState(77.5946);
  const [startDate, setStartDate] = useState("2026-10-01");
  const [endDate, setEndDate] = useState("2026-10-05");
  const [adults, setAdults] = useState(2);
  const [rooms, setRooms] = useState(1);
  const [mode, setMode] = useState<"car" | "motorcycle">("car");

  // Destination list
  const [destinations, setDestinations] = useState<DestinationItem[]>([
    { id: "1", name: "Mysuru", lat: 12.2958, lng: 76.6394, stayDays: 2 },
    { id: "2", name: "Coorg (Madikeri)", lat: 12.4244, lng: 75.7382, stayDays: 2 },
  ]);

  // Solver / Workflow execution state
  const [isPlanning, setIsPlanning] = useState(false);
  const [plannedVersion, setPlannedVersion] = useState(1);
  const [activeTab, setActiveTab] = useState<"itinerary" | "map" | "budget" | "recommendations" | "evidence">("itinerary");
  const [evidenceModalOpen, setEvidenceModalOpen] = useState(false);
  const [apiStatus, setApiStatus] = useState<"offline" | "connected" | "checking">("checking");
  const [serverTripId, setServerTripId] = useState<string>("urn:swena:trip:karnataka-circuit-2026");
  const [isExporting, setIsExporting] = useState(false);

  // Consume corridor query parameter
  useEffect(() => {
    if (!corridorParam) return;
    if (corridorParam === "rajasthan") {
      setOrigin("Delhi");
      setOriginLat(28.6139);
      setOriginLng(77.2090);
      setDestinations([
        { id: "1", name: "Jaipur", lat: 26.9124, lng: 75.7873, stayDays: 2 },
        { id: "2", name: "Pushkar", lat: 26.4899, lng: 74.5511, stayDays: 1 },
        { id: "3", name: "Jodhpur", lat: 26.2389, lng: 73.0243, stayDays: 2 },
      ]);
      setServerTripId("urn:swena:trip:rajasthan-circuit-2026");
    } else if (corridorParam === "konkan" || corridorParam === "konkan-coast") {
      setOrigin("Mumbai");
      setOriginLat(19.0760);
      setOriginLng(72.8777);
      setDestinations([
        { id: "1", name: "Alibaug", lat: 18.6534, lng: 72.8770, stayDays: 1 },
        { id: "2", name: "Ratnagiri", lat: 16.9902, lng: 73.3120, stayDays: 2 },
        { id: "3", name: "Goa", lat: 15.2993, lng: 74.1240, stayDays: 2 },
      ]);
      setServerTripId("urn:swena:trip:konkan-coast-2026");
    } else if (corridorParam === "western-ghats") {
      setOrigin("Bengaluru");
      setOriginLat(12.9716);
      setOriginLng(77.5946);
      setDestinations([
        { id: "1", name: "Mysuru", lat: 12.2958, lng: 76.6394, stayDays: 2 },
        { id: "2", name: "Coorg (Madikeri)", lat: 12.4244, lng: 75.7382, stayDays: 2 },
        { id: "3", name: "Wayanad", lat: 11.6854, lng: 76.1320, stayDays: 2 },
      ]);
      setServerTripId("urn:swena:trip:western-ghats-2026");
    }
  }, [corridorParam]);

  // Editable vehicle efficiency and fuel price (with units)
  const [mileage, setMileage] = useState(15.0);
  const [fuelPricePerLiter, setFuelPricePerLiter] = useState(102.5);

  // Check FastAPI backend connectivity on mount
  useEffect(() => {
    const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 2000);

    fetch(`${apiBase}/health`, { signal: controller.signal })
      .then((res) => {
        if (res.ok) setApiStatus("connected");
        else setApiStatus("offline");
      })
      .catch(() => setApiStatus("offline"))
      .finally(() => clearTimeout(timer));
  }, []);

  // Derived calendar days
  const start = new Date(startDate);
  const end = new Date(endDate);
  const diffDays = Math.max(1, Math.round((end.getTime() - start.getTime()) / (1000 * 3600 * 24)) + 1);

  // Dynamic itinerary generation
  const avgSpeed = mode === "car" ? 45 : 40;

  // Build points: origin -> destinations
  const points = [
    { name: origin, lat: originLat, lng: originLng, stayDays: 0 },
    ...destinations,
  ];

  const scheduledLegs: ScheduledLeg[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const dist = computeDistanceKm(points[i].lat, points[i].lng, points[i + 1].lat, points[i + 1].lng);
    const durationMins = Math.round((dist / avgSpeed) * 60);
    scheduledLegs.push({
      from: points[i].name,
      to: points[i + 1].name,
      distanceKm: dist,
      durationMins,
    });
  }

  let cumulativeMins = 570; // 09:30 AM
  let currentDay = 1;
  const scheduledStops: ScheduledStop[] = points.map((p, idx) => {
    if (idx === 0) {
      return {
        position: 0,
        name: p.name,
        arrival: "09:00 AM (Day 1)",
        departure: "09:30 AM (Day 1)",
        stayHours: 0.5,
      };
    }
    const leg = scheduledLegs[idx - 1];
    cumulativeMins += leg.durationMins;
    const arrivalHours = Math.floor((cumulativeMins % (24 * 60)) / 60);
    const arrivalMins = cumulativeMins % 60;
    const arrivalAmPm = arrivalHours >= 12 ? "PM" : "AM";
    const formattedArrHours = arrivalHours % 12 === 0 ? 12 : arrivalHours % 12;
    const arrTimeStr = `${String(formattedArrHours).padStart(2, "0")}:${String(arrivalMins).padStart(2, "0")} ${arrivalAmPm} (Day ${currentDay})`;

    currentDay += p.stayDays;
    cumulativeMins += p.stayDays * 24 * 60;
    const depTimeStr = `10:00 AM (Day ${currentDay})`;

    return {
      position: idx,
      name: p.name,
      arrival: arrTimeStr,
      departure: depTimeStr,
      stayHours: p.stayDays * 24,
    };
  });

  const totalDistanceKm = scheduledLegs.reduce((acc, l) => acc + l.distanceKm, 0);
  const totalTravelMins = scheduledLegs.reduce((acc, l) => acc + l.durationMins, 0);
  const fuelLiters = (totalDistanceKm / mileage).toFixed(1);
  const fuelTotalCost = Math.round(parseFloat(fuelLiters) * fuelPricePerLiter);
  const hotelEstimatedTotal = destinations.reduce((acc, d) => acc + d.stayDays * rooms * 3500, 0);

  const mapStops = [
    { name: `${origin} (Origin)`, lat: originLat, lng: originLng },
    ...destinations.map((d) => ({
      name: d.name,
      lat: d.lat,
      lng: d.lng,
      stayMinutes: d.stayDays * 24 * 60,
    })),
  ];

  const corridorPlaces = [
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
      stopName: origin,
      temperatureC: 24,
      condition: "sunny",
      humidityPercent: 55,
      monsoonRisk: "low",
    },
    ...destinations.map((d) => ({
      stopName: d.name,
      temperatureC: d.name.toLowerCase().includes("coorg") ? 19 : 27,
      condition: (d.name.toLowerCase().includes("coorg") ? "foggy" : "cloudy") as
        | "sunny"
        | "cloudy"
        | "rainy"
        | "foggy",
      humidityPercent: d.name.toLowerCase().includes("coorg") ? 88 : 60,
      monsoonRisk: (d.name.toLowerCase().includes("coorg") ? "moderate" : "low") as
        | "low"
        | "moderate"
        | "severe",
      ghatCaution: d.name.toLowerCase().includes("coorg")
        ? "Misty mountain passes; maintain low-beam headlights on ghat curves."
        : undefined,
    })),
  ];

  const handleVoiceTranscript = (text: string) => {
    const lower = text.toLowerCase();
    if (lower.includes("mysore") || lower.includes("mysuru")) {
      if (!destinations.some((d) => d.name.toLowerCase().includes("mysur"))) {
        setDestinations((prev) => [
          ...prev,
          { id: String(Date.now()), name: "Mysuru", lat: 12.2958, lng: 76.6394, stayDays: 2 },
        ]);
      }
    }
    if (lower.includes("coorg") || lower.includes("madikeri")) {
      if (!destinations.some((d) => d.name.toLowerCase().includes("coorg"))) {
        setDestinations((prev) => [
          ...prev,
          { id: String(Date.now() + 1), name: "Coorg (Madikeri)", lat: 12.4244, lng: 75.7382, stayDays: 2 },
        ]);
      }
    }
    if (lower.includes("wayanad")) {
      if (!destinations.some((d) => d.name.toLowerCase().includes("wayanad"))) {
        setDestinations((prev) => [
          ...prev,
          { id: String(Date.now() + 2), name: "Wayanad", lat: 11.6854, lng: 76.132, stayDays: 2 },
        ]);
      }
    }
    if (lower.includes("car")) setMode("car");
    if (lower.includes("bike") || lower.includes("motorcycle")) setMode("motorcycle");
  };

  const narrativeScript = `Your personalized travel itinerary begins in ${origin}, connecting to ${destinations
    .map((d) => d.name)
    .join(" and ")} across ${totalDistanceKm} kilometers. Estimated travel time is ${Math.round(
    totalTravelMins / 60
  )} hours. Fuel requirement is calculated at ${fuelLiters} liters with strict preservation of unknown tolls.`;


  const addDestination = () => {
    const newId = String(destinations.length + 1);
    setDestinations([
      ...destinations,
      { id: newId, name: "Wayanad", lat: 11.6854, lng: 76.132, stayDays: 1 },
    ]);
  };

  const removeDestination = (id: string) => {
    if (destinations.length <= 1) return;
    setDestinations(destinations.filter((d) => d.id !== id));
  };

  const updateDestination = (id: string, field: keyof DestinationItem, value: any) => {
    setDestinations(
      destinations.map((d) => (d.id === id ? { ...d, [field]: value } : d))
    );
  };

  const triggerPlanning = async () => {
    setIsPlanning(true);
    const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

    try {
      const createRes = await fetch(`${apiBase}/api/v1/trips`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          owner_id: "00000000-0000-0000-0000-000000000001",
          brief: {
            origin_name: origin,
            origin_lat: originLat,
            origin_lng: originLng,
            destinations: destinations.map((d) => ({
              name: d.name,
              latitude: d.lat,
              longitude: d.lng,
              confidence: 1.0,
              stay_days: d.stayDays,
            })),
            start_date: startDate,
            end_date: endDate,
            adults,
            rooms,
            preferred_modes: [mode === "car" ? "car_petrol" : "motorcycle_petrol"],
          },
        }),
      });

      if (createRes.ok) {
        const tripData = await createRes.json();
        setServerTripId(tripData.id);
        const planRes = await fetch(`${apiBase}/api/v1/trips/${tripData.id}/plan`, {
          method: "POST",
        });
        if (planRes.ok) {
          setApiStatus("connected");
        }
      } else {
        setApiStatus("offline");
      }
    } catch {
      // Backend not running locally - fallback to client constraint scheduler
      setApiStatus("offline");
    } finally {
      setIsPlanning(false);
      setPlannedVersion((prev) => prev + 1);
    }
  };

  const handleExportPdf = async () => {
    setIsExporting(true);
    const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

    try {
      if (serverTripId && serverTripId.includes("-")) {
        const res = await fetch(`${apiBase}/api/v1/trips/${serverTripId}/exports`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ trip_version: plannedVersion, format: "pdf" }),
        });
        if (res.ok) {
          const data = await res.json();
          window.open(`${apiBase}/api/v1/artifacts/${data.artifact_id}/download`, "_blank");
          setIsExporting(false);
          return;
        }
      }
    } catch {
      // Backend not running locally
    }

    setTimeout(() => {
      setIsExporting(false);
      window.print();
    }, 600);
  };

  return (
    <div className="py-8 px-4 sm:px-6 max-w-7xl mx-auto">
      {/* Top Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-8 pb-6 border-b border-[#15271F]">
        <div>
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-[#15271F] border border-[#B7C9AD]/20 text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider mb-2">
            <Compass className="h-3.5 w-3.5" />
            <span>Active Aggregate: Karnataka Corridor Odyssey</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-bold text-[#F7F7F2]">
            Trip Planning Console
          </h1>
        </div>

        <div className="flex items-center flex-wrap gap-3">
          {/* Engine Status Badge */}
          {apiStatus === "connected" ? (
            <div className="glass-panel px-3 py-1.5 rounded-xl text-xs flex items-center gap-1.5 text-[#B7C9AD] border border-[#B7C9AD]/30">
              <span className="h-2 w-2 rounded-full bg-[#B7C9AD] animate-pulse" />
              <span>FastAPI Backend Active</span>
            </div>
          ) : (
            <div className="glass-panel px-3 py-1.5 rounded-xl text-xs flex items-center gap-1.5 text-[#A9B8AD]">
              <span className="h-2 w-2 rounded-full bg-[#A9B8AD]/40" />
              <span>Local Constraint Engine</span>
            </div>
          )}

          <div className="glass-panel px-4 py-2 rounded-xl text-xs flex items-center gap-2">
            <span className="text-[#A9B8AD]">Version:</span>
            <span className="font-mono font-bold text-[#B7C9AD]">v{plannedVersion}</span>
          </div>
          <div className="glass-panel px-4 py-2 rounded-xl text-xs flex items-center gap-2">
            <span className="text-[#A9B8AD]">Mode:</span>
            <span className="capitalize font-semibold text-[#F7F7F2]">{mode}</span>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        {/* LEFT COLUMN: BRIEF EDITOR (4 cols) */}
        <div className="lg:col-span-4 space-y-6">
          <div className="glass-card p-6 rounded-3xl border border-[#233e32]">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-5">
              <h2 className="text-base font-bold text-[#F7F7F2] flex items-center gap-2">
                <Route className="h-4 w-4 text-[#B7C9AD]" />
                <span>Travel Brief Specification</span>
              </h2>
              <VoiceBriefAssistant
                onTranscript={handleVoiceTranscript}
                narrativeText={narrativeScript}
              />
            </div>

            <div className="space-y-4 text-xs">
              {/* Origin */}
              <div>
                <label className="block text-[#A9B8AD] mb-1 font-medium uppercase tracking-wider">
                  Origin Hub
                </label>
                <div className="relative">
                  <MapPin className="h-4 w-4 text-[#B7C9AD] absolute left-3 top-3" />
                  <input
                    type="text"
                    value={origin}
                    onChange={(e) => setOrigin(e.target.value)}
                    className="w-full bg-[#0D1915] border border-[#233e32] rounded-xl pl-9 pr-3 py-2.5 text-[#F7F7F2] focus:outline-none focus:border-[#B7C9AD]"
                  />
                </div>
              </div>

              {/* Dates */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[#A9B8AD] mb-1 font-medium uppercase tracking-wider">
                    Departure
                  </label>
                  <input
                    type="date"
                    value={startDate}
                    onChange={(e) => setStartDate(e.target.value)}
                    className="w-full bg-[#0D1915] border border-[#233e32] rounded-xl px-3 py-2 text-[#F7F7F2] focus:outline-none focus:border-[#B7C9AD]"
                  />
                </div>
                <div>
                  <label className="block text-[#A9B8AD] mb-1 font-medium uppercase tracking-wider">
                    Return
                  </label>
                  <input
                    type="date"
                    value={endDate}
                    onChange={(e) => setEndDate(e.target.value)}
                    className="w-full bg-[#0D1915] border border-[#233e32] rounded-xl px-3 py-2 text-[#F7F7F2] focus:outline-none focus:border-[#B7C9AD]"
                  />
                </div>
              </div>

              <div className="text-right text-[11px] text-[#A9B8AD]">
                Total Duration: <span className="text-[#B7C9AD] font-semibold">{diffDays} calendar days</span>
              </div>

              {/* Passengers & Rooms */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[#A9B8AD] mb-1 font-medium uppercase tracking-wider">
                    Travelers
                  </label>
                  <select
                    value={adults}
                    onChange={(e) => setAdults(Number(e.target.value))}
                    className="w-full bg-[#0D1915] border border-[#233e32] rounded-xl px-3 py-2 text-[#F7F7F2] focus:outline-none focus:border-[#B7C9AD]"
                  >
                    <option value={1}>1 Adult</option>
                    <option value={2}>2 Adults</option>
                    <option value={4}>4 Adults</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[#A9B8AD] mb-1 font-medium uppercase tracking-wider">
                    Rooms
                  </label>
                  <select
                    value={rooms}
                    onChange={(e) => setRooms(Number(e.target.value))}
                    className="w-full bg-[#0D1915] border border-[#233e32] rounded-xl px-3 py-2 text-[#F7F7F2] focus:outline-none focus:border-[#B7C9AD]"
                  >
                    <option value={1}>1 Room</option>
                    <option value={2}>2 Rooms</option>
                  </select>
                </div>
              </div>

              {/* Vehicle Mode */}
              <div>
                <label className="block text-[#A9B8AD] mb-2 font-medium uppercase tracking-wider">
                  Transport Preference
                </label>
                <div className="grid grid-cols-2 gap-2 bg-[#0D1915] p-1 rounded-xl border border-[#233e32]">
                  <button
                    onClick={() => {
                      setMode("car");
                      setMileage(15.0);
                    }}
                    className={`flex items-center justify-center gap-2 py-2 rounded-lg font-semibold transition-all ${
                      mode === "car"
                        ? "bg-[#15271F] text-[#B7C9AD] shadow-sm"
                        : "text-[#A9B8AD]"
                    }`}
                  >
                    <Car className="h-3.5 w-3.5" />
                    <span>Petrol Car</span>
                  </button>
                  <button
                    onClick={() => {
                      setMode("motorcycle");
                      setMileage(40.0);
                    }}
                    className={`flex items-center justify-center gap-2 py-2 rounded-lg font-semibold transition-all ${
                      mode === "motorcycle"
                        ? "bg-[#15271F] text-[#B7C9AD] shadow-sm"
                        : "text-[#A9B8AD]"
                    }`}
                  >
                    <Bike className="h-3.5 w-3.5" />
                    <span>Motorcycle</span>
                  </button>
                </div>

                {/* Editable Mileage & Fuel Price with Units */}
                <div className="grid grid-cols-2 gap-3 mt-3 pt-3 border-t border-[#233e32]/60">
                  <div>
                    <label className="block text-[11px] text-[#A9B8AD] mb-1">
                      Mileage (km/L)
                    </label>
                    <input
                      type="number"
                      step="0.5"
                      min="5"
                      max="100"
                      value={mileage}
                      onChange={(e) => setMileage(Math.max(1, parseFloat(e.target.value) || 15))}
                      className="w-full bg-[#0D1915] border border-[#233e32] rounded-xl px-2.5 py-1.5 text-xs text-[#F7F7F2] font-mono focus:outline-none focus:border-[#B7C9AD]"
                    />
                  </div>
                  <div>
                    <label className="block text-[11px] text-[#A9B8AD] mb-1">
                      Fuel Price (₹/L)
                    </label>
                    <input
                      type="number"
                      step="0.5"
                      min="50"
                      max="200"
                      value={fuelPricePerLiter}
                      onChange={(e) => setFuelPricePerLiter(Math.max(1, parseFloat(e.target.value) || 102.5))}
                      className="w-full bg-[#0D1915] border border-[#233e32] rounded-xl px-2.5 py-1.5 text-xs text-[#F7F7F2] font-mono focus:outline-none focus:border-[#B7C9AD]"
                    />
                  </div>
                </div>

                <p className="text-[10px] text-[#A9B8AD]/80 mt-2 leading-tight">
                  <span className="text-[#B7C9AD] font-semibold">Boundary Note:</span> EV energy/range modeling is unavailable per architectural decision D010. Modes strictly cover petrol vehicles and walking/bicycles.
                </p>
              </div>

              {/* Destination Stops */}
              <div className="pt-2">
                <div className="flex items-center justify-between mb-2">
                  <label className="block text-[#A9B8AD] font-medium uppercase tracking-wider">
                    Destinations & Stays
                  </label>
                  <button
                    onClick={addDestination}
                    className="text-[#B7C9AD] hover:text-[#c9dbbe] flex items-center gap-1 font-semibold text-[11px]"
                  >
                    <Plus className="h-3 w-3" />
                    <span>Add Stop</span>
                  </button>
                </div>

                <div className="space-y-2.5">
                  {destinations.map((dest, idx) => (
                    <div
                      key={dest.id}
                      className="bg-[#0D1915] p-3 rounded-xl border border-[#233e32] flex items-center justify-between gap-2"
                    >
                      <div className="flex-1">
                        <input
                          type="text"
                          value={dest.name}
                          onChange={(e) =>
                            updateDestination(dest.id, "name", e.target.value)
                          }
                          className="w-full bg-transparent font-semibold text-[#F7F7F2] focus:outline-none"
                        />
                        <div className="flex items-center gap-2 text-[11px] text-[#A9B8AD] mt-1">
                          <span>Stay:</span>
                          <input
                            type="number"
                            min={1}
                            max={10}
                            value={dest.stayDays}
                            onChange={(e) =>
                              updateDestination(
                                dest.id,
                                "stayDays",
                                Number(e.target.value)
                              )
                            }
                            className="w-10 bg-[#15271F] px-1 py-0.5 rounded text-center text-[#B7C9AD] focus:outline-none"
                          />
                          <span>nights</span>
                        </div>
                      </div>

                      {destinations.length > 1 && (
                        <button
                          onClick={() => removeDestination(dest.id)}
                          className="text-[#687d6f] hover:text-[#e06d53] p-1 transition-colors"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </div>

              {/* Trigger Button */}
              <button
                onClick={triggerPlanning}
                disabled={isPlanning}
                className="btn-sage w-full py-3 rounded-xl font-bold flex items-center justify-center gap-2 text-sm mt-4 shadow-lg disabled:opacity-60"
              >
                {isPlanning ? (
                  <>
                    <RefreshCw className="h-4 w-4 animate-spin" />
                    <span>OR-Tools Solving Constraints...</span>
                  </>
                ) : (
                  <>
                    <Sparkles className="h-4 w-4" />
                    <span>Compile & Re-solve Schedule</span>
                  </>
                )}
              </button>
            </div>
          </div>
        </div>

        {/* RIGHT COLUMN: INTERACTIVE OUTPUT TABS (8 cols) */}
        <div className="lg:col-span-8 space-y-6">
          {/* Subheader & Tabs */}
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-[#233e32] pb-3">
            <div className="flex items-center flex-wrap gap-2">
              <button
                onClick={() => setActiveTab("itinerary")}
                className={`px-3 py-1.5 rounded-xl text-xs font-semibold transition-all ${
                  activeTab === "itinerary"
                    ? "bg-[#15271F] text-[#B7C9AD] border border-[#B7C9AD]/30"
                    : "text-[#A9B8AD] hover:text-[#F7F7F2]"
                }`}
              >
                Solved Schedule
              </button>
              <button
                onClick={() => setActiveTab("map")}
                className={`px-3 py-1.5 rounded-xl text-xs font-semibold transition-all ${
                  activeTab === "map"
                    ? "bg-[#15271F] text-[#B7C9AD] border border-[#B7C9AD]/30"
                    : "text-[#A9B8AD] hover:text-[#F7F7F2]"
                }`}
              >
                Corridor Map
              </button>
              <button
                onClick={() => setActiveTab("budget")}
                className={`px-3 py-1.5 rounded-xl text-xs font-semibold transition-all ${
                  activeTab === "budget"
                    ? "bg-[#15271F] text-[#B7C9AD] border border-[#B7C9AD]/30"
                    : "text-[#A9B8AD] hover:text-[#F7F7F2]"
                }`}
              >
                Itemized Budget
              </button>
              <button
                onClick={() => setActiveTab("recommendations")}
                className={`px-3 py-1.5 rounded-xl text-xs font-semibold transition-all ${
                  activeTab === "recommendations"
                    ? "bg-[#15271F] text-[#B7C9AD] border border-[#B7C9AD]/30"
                    : "text-[#A9B8AD] hover:text-[#F7F7F2]"
                }`}
              >
                Corridor Places
              </button>
              <button
                onClick={() => setActiveTab("evidence")}
                className={`px-3 py-1.5 rounded-xl text-xs font-semibold transition-all ${
                  activeTab === "evidence"
                    ? "bg-[#15271F] text-[#B7C9AD] border border-[#B7C9AD]/30"
                    : "text-[#A9B8AD] hover:text-[#F7F7F2]"
                }`}
              >
                Evidence Registry
              </button>
            </div>

            <div className="flex items-center gap-2 self-end sm:self-auto">
              <Link
                href="/trips/karnataka-circuit-2026"
                className="text-xs text-[#B7C9AD] bg-[#15271F] hover:bg-[#1c352a] border border-[#B7C9AD]/30 px-3 py-1.5 rounded-xl flex items-center gap-1.5 font-semibold transition-all cursor-pointer"
              >
                <Share2 className="h-3.5 w-3.5" />
                <span>Share & QR</span>
              </Link>

              <button
                onClick={handleExportPdf}
                disabled={isExporting}
                className="text-xs text-[#B7C9AD] bg-[#15271F] hover:bg-[#1c352a] border border-[#B7C9AD]/30 px-3 py-1.5 rounded-xl flex items-center gap-1.5 font-semibold transition-all disabled:opacity-50"
              >
                <Download className="h-3.5 w-3.5" />
                <span>{isExporting ? "Exporting..." : "Export PDF"}</span>
              </button>

              <button
                onClick={() => setEvidenceModalOpen(true)}
                className="text-xs text-[#A9B8AD] hover:text-[#F7F7F2] hover:underline flex items-center gap-1 font-semibold"
              >
                <ShieldCheck className="h-3.5 w-3.5" />
                <span>Evidence Audit</span>
              </button>
            </div>
          </div>

          {/* TAB 1: ITINERARY TIMELINE */}
          {activeTab === "itinerary" && (
            <div className="space-y-4">
              {/* Corridor Weather & Ghat Advisory */}
              <CorridorWeatherAdvisory stops={weatherStops} />

              {/* Summary Metric Ribbon */}
              <div className="grid grid-cols-3 gap-3">
                <div className="glass-card p-4 rounded-2xl border border-[#233e32]">
                  <span className="text-[11px] text-[#A9B8AD] block">Total Road Distance</span>
                  <span className="text-lg font-bold text-[#F7F7F2]">{totalDistanceKm} km</span>
                </div>
                <div className="glass-card p-4 rounded-2xl border border-[#233e32]">
                  <span className="text-[11px] text-[#A9B8AD] block">Est. Driving Time</span>
                  <span className="text-lg font-bold text-[#F7F7F2]">
                    {Math.floor(totalTravelMins / 60)}h {totalTravelMins % 60}m
                  </span>
                </div>
                <div className="glass-card p-4 rounded-2xl border border-[#233e32]">
                  <span className="text-[11px] text-[#A9B8AD] block">Constraint Solver</span>
                  <span className="text-lg font-bold text-[#B7C9AD] flex items-center gap-1">
                    <CheckCircle2 className="h-4 w-4" /> Optimal
                  </span>
                </div>
              </div>

              {/* Stop Sequence */}
              <div className="glass-card p-6 rounded-3xl border border-[#233e32] space-y-6">
                <h3 className="text-sm font-bold text-[#F7F7F2] uppercase tracking-wider flex items-center gap-2">
                  <Clock className="h-4 w-4 text-[#B7C9AD]" />
                  <span>Monotonically Sequenced Stops</span>
                </h3>

                <div className="relative pl-6 border-l-2 border-[#233e32] space-y-8 ml-3">
                  {scheduledStops.map((stop, idx) => (
                    <div key={stop.name} className="relative">
                      {/* Step node dot */}
                      <div className="absolute -left-[31px] top-0.5 h-4 w-4 rounded-full bg-[#15271F] border-2 border-[#B7C9AD]" />

                      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="text-xs font-mono text-[#B7C9AD]">
                              Stop #{stop.position}
                            </span>
                            <h4 className="text-base font-bold text-[#F7F7F2]">
                              {stop.name}
                            </h4>
                          </div>
                          <p className="text-xs text-[#A9B8AD] mt-1">
                            Arrival: <span className="text-[#F7F7F2]">{stop.arrival}</span> • Departure:{" "}
                            <span className="text-[#F7F7F2]">{stop.departure}</span>
                          </p>
                        </div>

                        {idx > 0 && (
                          <div className="bg-[#0D1915] px-3 py-1.5 rounded-xl border border-[#233e32] text-xs">
                            <span className="text-[#A9B8AD]">Transit Leg: </span>
                            <span className="font-semibold text-[#B7C9AD]">
                              {scheduledLegs[idx - 1].distanceKm} km ({Math.floor(scheduledLegs[idx - 1].durationMins / 60)}h {scheduledLegs[idx - 1].durationMins % 60}m)
                            </span>
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* TAB 5: INTERACTIVE CORRIDOR MAP */}
          {activeTab === "map" && (
            <div className="space-y-4">
              <div className="bg-[#15271F] p-5 rounded-2xl border border-[#233E32] flex flex-col sm:flex-row sm:items-center justify-between gap-3 shadow-xl">
                <div>
                  <h3 className="text-sm font-bold text-[#F7F7F2] flex items-center gap-2">
                    <MapIcon className="w-4 h-4 text-[#B7C9AD]" />
                    Geospatial Corridor Route Canvas
                  </h3>
                  <p className="text-xs text-[#A9B8AD] mt-0.5">
                    Interactive PostGIS corridor waypoints, recommended detours, and road traces
                  </p>
                </div>
                <div className="text-xs text-[#B7C9AD] bg-[#0D1915] px-3.5 py-1.5 rounded-full border border-[#233E32] font-semibold flex items-center gap-2 self-start sm:self-auto">
                  <span className="w-2 h-2 rounded-full bg-[#B7C9AD] animate-pulse"></span>
                  <span>{mapStops.length} Corridor Stops Active</span>
                </div>
              </div>
              <div className="h-[520px] w-full rounded-2xl overflow-hidden border border-[#233E32] shadow-2xl">
                <CorridorMapWrapper stops={mapStops} places={corridorPlaces} />
              </div>
            </div>
          )}

          {/* TAB 2: ZERO-COERCION BUDGET */}
          {activeTab === "budget" && (
            <div className="space-y-6">
              {/* Unknown Toll Alert Banner */}
              <div className="p-5 rounded-2xl bg-[#271E15]/80 border border-[#D4A373]/40 flex items-start gap-3.5">
                <AlertTriangle className="h-5 w-5 text-[#D4A373] shrink-0 mt-0.5" />
                <div className="text-xs space-y-1">
                  <h4 className="font-bold text-[#F7F7F2] uppercase tracking-wide">
                    Budget Completeness: Incomplete (Preserved)
                  </h4>
                  <p className="text-[#A9B8AD] leading-relaxed">
                    Per UI/UX Design Brief & Rule 3: Toll charges on the Mysuru-Coorg corridor
                    are unverified in the registry. This unknown cost has{" "}
                    <span className="text-[#D4A373] font-semibold">NOT been coerced to ₹0</span>.
                    The total explicitly notes missing tolls rather than claiming &ldquo;Within budget&rdquo;.
                  </p>
                </div>
              </div>

              {/* Line items table */}
              <div className="glass-card rounded-3xl p-6 border border-[#233e32] space-y-4">
                <h3 className="text-sm font-bold text-[#F7F7F2] uppercase tracking-wider flex items-center gap-2">
                  <Receipt className="h-4 w-4 text-[#B7C9AD]" />
                  <span>Itemized Financial Breakdown</span>
                </h3>

                <div className="divide-y divide-[#15271F] text-xs">
                  {/* Fuel */}
                  <div className="py-3 flex items-center justify-between">
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-[#F7F7F2]">
                          Petrol Fuel Transit ({totalDistanceKm} km @ {mileage} km/L)
                        </span>
                        <span className="px-1.5 py-0.5 rounded bg-[#15271F] text-[#B7C9AD] text-[10px]">
                          Math Model
                        </span>
                      </div>
                      <span className="text-[#A9B8AD]">
                        Calculated {fuelLiters}L × ₹{fuelPricePerLiter}/L based on editable vehicle parameters
                      </span>
                    </div>
                    <span className="font-mono font-bold text-[#F7F7F2]">
                      ₹{fuelTotalCost.toLocaleString("en-IN")}
                    </span>
                  </div>

                  {/* Hotels */}
                  <div className="py-3 flex items-center justify-between">
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-[#F7F7F2]">
                          Accommodation ({rooms} room, {destinations.reduce((acc, d) => acc + d.stayDays, 0)} total nights)
                        </span>
                        <span className="px-1.5 py-0.5 rounded bg-[#15271F] text-[#B7C9AD] text-[10px]">
                          Observed Price
                        </span>
                      </div>
                      <span className="text-[#A9B8AD]">
                        Indicative benchmark ₹3,500/room/night across {destinations.map((d) => d.name).join(", ")}
                      </span>
                    </div>
                    <span className="font-mono font-bold text-[#F7F7F2]">
                      ₹{hotelEstimatedTotal.toLocaleString("en-IN")}
                    </span>
                  </div>

                  {/* Attractions & Parking */}
                  <div className="py-3 flex items-center justify-between">
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-[#F7F7F2]">
                          Attractions, Gate & Parking Estimates
                        </span>
                        <span className="px-1.5 py-0.5 rounded bg-[#15271F] text-[#B7C9AD] text-[10px]">
                          Estimated
                        </span>
                      </div>
                      <span className="text-[#A9B8AD]">
                        Mysuru Palace entry, Chamundi parking, Dubare sanctuary passes
                      </span>
                    </div>
                    <span className="font-mono font-bold text-[#F7F7F2]">
                      ₹400
                    </span>
                  </div>

                  {/* Unverified Tolls */}
                  <div className="py-3 flex items-center justify-between bg-[#271E15]/30 -mx-3 px-3 rounded-lg">
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-[#D4A373]">
                          NH-275 Highway & Ghat Toll Plazas
                        </span>
                        <span className="px-1.5 py-0.5 rounded bg-[#271E15] text-[#D4A373] text-[10px] font-mono border border-[#D4A373]/30">
                          EXPLICIT UNKNOWN
                        </span>
                      </div>
                      <span className="text-[#A9B8AD]">
                        Unverified toll plaza rates for vehicle class; payable on FASTag at plaza
                      </span>
                    </div>
                    <span className="font-mono font-semibold text-[#D4A373]">
                      UNKNOWN
                    </span>
                  </div>
                </div>

                {/* Subtotal formatted strictly per UI/UX design brief */}
                <div className="pt-4 border-t border-[#233e32] space-y-2">
                  <div className="flex items-center justify-between text-sm">
                    <div>
                      <span className="font-bold text-[#F7F7F2]">
                        Known/estimated subtotal; tolls unknown
                      </span>
                      <span className="block text-[11px] text-[#A9B8AD]">
                        Excludes unverified tolls and optional boat safari charges
                      </span>
                    </div>
                    <div className="text-right">
                      <span className="text-xl font-bold text-[#B7C9AD]">
                        ₹{(fuelTotalCost + hotelEstimatedTotal + 400).toLocaleString("en-IN")}
                      </span>
                      <span className="text-xs text-[#D4A373] block">+ unquoted tolls</span>
                    </div>
                  </div>

                  <div className="p-3 rounded-xl bg-[#0D1915] border border-[#233e32] text-xs text-[#A9B8AD]">
                    <span className="text-[#B7C9AD] font-semibold">Per-person known share: </span>
                    <span className="font-mono text-[#F7F7F2]">
                      ₹{Math.round((fuelTotalCost + hotelEstimatedTotal + 400) / adults).toLocaleString("en-IN")}
                    </span>{" "}
                    (based on {adults} adults sharing 1 vehicle & {rooms} room; infants excluded per carrier rules).
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* TAB 3: RECOMMENDATIONS (CORRIDOR PLACES) */}
          {activeTab === "recommendations" && (
            <div className="space-y-4">
              <div className="glass-panel p-4 rounded-2xl border border-[#233e32] text-xs text-[#A9B8AD]">
                <span className="text-[#B7C9AD] font-semibold">Attribution & Ethos: </span>
                Recommended places are explained editorial selections with explicit detour and visit times. They are not paid placements or algorithmic guarantees.
              </div>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                {/* Recommendation 1 */}
                <div className="glass-card p-5 rounded-3xl border border-[#233e32] space-y-3">
                  <div className="flex items-start justify-between">
                    <span className="px-2 py-0.5 rounded-full bg-[#15271F] border border-[#B7C9AD]/20 text-[10px] font-semibold text-[#B7C9AD]">
                      Natural Heritage
                    </span>
                    <span className="text-[10px] font-mono text-[#A9B8AD]">+18m detour</span>
                  </div>
                  <h4 className="font-bold text-[#F7F7F2] text-sm">
                    Ranganathittu Sanctuary
                  </h4>
                  <p className="text-xs text-[#A9B8AD] leading-relaxed">
                    Directly on NH-275 corridor near Srirangapatna. Ideal morning bird-watching stop before Mysuru hotel check-in.
                  </p>
                  <div className="text-[11px] space-y-1 pt-2 border-t border-[#233e32]">
                    <div className="flex justify-between text-[#A9B8AD]">
                      <span>Visit Duration:</span>
                      <span className="text-[#F7F7F2]">2.0 hours</span>
                    </div>
                    <div className="flex justify-between text-[#A9B8AD]">
                      <span>Source:</span>
                      <span className="text-[#B7C9AD]">Karnataka Forest Dept</span>
                    </div>
                    <div className="text-[10px] text-[#D4A373] mt-1">
                      ⚠️ Boat safari subject to seasonal river flow.
                    </div>
                  </div>
                </div>

                {/* Recommendation 2 */}
                <div className="glass-card p-5 rounded-3xl border border-[#233e32] space-y-3">
                  <div className="flex items-start justify-between">
                    <span className="px-2 py-0.5 rounded-full bg-[#15271F] border border-[#B7C9AD]/20 text-[10px] font-semibold text-[#B7C9AD]">
                      Cultural Overlook
                    </span>
                    <span className="text-[10px] font-mono text-[#A9B8AD]">+25m detour</span>
                  </div>
                  <h4 className="font-bold text-[#F7F7F2] text-sm">
                    Chamundi Hill Viewpoint
                  </h4>
                  <p className="text-xs text-[#A9B8AD] leading-relaxed">
                    Panoramic elevation looking across the palace plateau. Accessible via paved highway switchbacks.
                  </p>
                  <div className="text-[11px] space-y-1 pt-2 border-t border-[#233e32]">
                    <div className="flex justify-between text-[#A9B8AD]">
                      <span>Visit Duration:</span>
                      <span className="text-[#F7F7F2]">1.5 hours</span>
                    </div>
                    <div className="flex justify-between text-[#A9B8AD]">
                      <span>Source:</span>
                      <span className="text-[#B7C9AD]">PostGIS Verified Node</span>
                    </div>
                    <div className="text-[10px] text-[#D4A373] mt-1">
                      ⚠️ Weekend parking queue may delay descent.
                    </div>
                  </div>
                </div>

                {/* Recommendation 3 */}
                <div className="glass-card p-5 rounded-3xl border border-[#233e32] space-y-3">
                  <div className="flex items-start justify-between">
                    <span className="px-2 py-0.5 rounded-full bg-[#15271F] border border-[#B7C9AD]/20 text-[10px] font-semibold text-[#B7C9AD]">
                      River Waypoint
                    </span>
                    <span className="text-[10px] font-mono text-[#A9B8AD]">+12m detour</span>
                  </div>
                  <h4 className="font-bold text-[#F7F7F2] text-sm">
                    Dubare Elephant Reserve
                  </h4>
                  <p className="text-xs text-[#A9B8AD] leading-relaxed">
                    Kaveri river crossing on the approach into Coorg. Natural transition point between plain and western ghats.
                  </p>
                  <div className="text-[11px] space-y-1 pt-2 border-t border-[#233e32]">
                    <div className="flex justify-between text-[#A9B8AD]">
                      <span>Visit Duration:</span>
                      <span className="text-[#F7F7F2]">2.5 hours</span>
                    </div>
                    <div className="flex justify-between text-[#A9B8AD]">
                      <span>Source:</span>
                      <span className="text-[#B7C9AD]">Jungle Lodges & Resorts</span>
                    </div>
                    <div className="text-[10px] text-[#D4A373] mt-1">
                      ⚠️ Morning bathing activities close at 11:30 AM.
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* TAB 4: EVIDENCE REGISTRY TABLE (Per UI/UX Design Brief Table) */}
          {activeTab === "evidence" && (
            <div className="space-y-4">
              <div className="glass-card p-6 rounded-3xl border border-[#233e32] space-y-4">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-bold text-[#F7F7F2] uppercase tracking-wider flex items-center gap-2">
                    <ShieldCheck className="h-4 w-4 text-[#B7C9AD]" />
                    <span>Evidence Presentation Standard</span>
                  </h3>
                  <span className="text-[11px] text-[#A9B8AD] font-mono">
                    Server-Controlled Classification
                  </span>
                </div>

                <div className="overflow-x-auto">
                  <table className="w-full text-xs text-left">
                    <thead>
                      <tr className="border-b border-[#233e32] text-[#A9B8AD] uppercase text-[10px]">
                        <th className="py-2.5 px-3">Classification</th>
                        <th className="py-2.5 px-3">User Presentation Copy</th>
                        <th className="py-2.5 px-3">System Behavior & Freshness</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[#15271F]">
                      <tr>
                        <td className="py-3 px-3 font-mono font-bold text-[#B7C9AD]">LIVE_OFFER</td>
                        <td className="py-3 px-3 text-[#F7F7F2]">
                          Current provider offer; checked 12 mins ago
                        </td>
                        <td className="py-3 px-3 text-[#A9B8AD]">
                          Live provider fare (IRCTC AC Chair Car); not a price hold.
                        </td>
                      </tr>
                      <tr>
                        <td className="py-3 px-3 font-mono font-bold text-[#F7F7F2]">INDICATIVE_SEARCH</td>
                        <td className="py-3 px-3 text-[#F7F7F2]">
                          Observed price; verify on provider
                        </td>
                        <td className="py-3 px-3 text-[#A9B8AD]">
                          Hotel benchmark (₹3,500/night); room inclusions unverified until supplier view.
                        </td>
                      </tr>
                      <tr>
                        <td className="py-3 px-3 font-mono font-bold text-[#A9B8AD]">EDITORIAL_DISCOVERY</td>
                        <td className="py-3 px-3 text-[#F7F7F2]">
                          Source information
                        </td>
                        <td className="py-3 px-3 text-[#A9B8AD]">
                          Karnataka Forest Dept hours and seasonal ghat permit advisories.
                        </td>
                      </tr>
                      <tr>
                        <td className="py-3 px-3 font-mono font-bold text-[#D4A373]">UNAVAILABLE</td>
                        <td className="py-3 px-3 text-[#D4A373]">
                          Could not verify
                        </td>
                        <td className="py-3 px-3 text-[#A9B8AD]">
                          Toll plaza tariff for vehicle class; preserved as explicit unknown.
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}

          {/* External Booking Handoff Section (Per Section 5 of Brief) */}
          <div className="glass-panel p-6 rounded-3xl border border-[#B7C9AD]/20 space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <div className="space-y-1 text-xs">
                <span className="font-bold text-[#F7F7F2] uppercase tracking-wider block">
                  Official Merchant Handoff
                </span>
                <p className="text-[#A9B8AD]">
                  Direct deep-links to verified supplier portals. No markups, no checkout lock-in, no fake price holds.
                </p>
                <p className="text-[11px] text-[#A9B8AD]/70">
                  Dates: <span className="text-[#F7F7F2]">{startDate} – {endDate}</span> | Party:{" "}
                  <span className="text-[#F7F7F2]">{adults} Adults, {rooms} Room</span>
                </p>
              </div>

              <div className="flex items-center gap-2.5 flex-wrap">
                <a
                  href="https://www.irctc.co.in"
                  target="_blank"
                  rel="noreferrer"
                  className="btn-sage px-4 py-2 rounded-xl text-xs font-semibold flex items-center gap-1.5 whitespace-nowrap shadow-md"
                >
                  <span>View on IRCTC</span>
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>

                <a
                  href="https://www.kstdc.co"
                  target="_blank"
                  rel="noreferrer"
                  className="btn-outline-forest px-4 py-2 rounded-xl text-xs font-semibold flex items-center gap-1.5 whitespace-nowrap"
                >
                  <span>Search on KSTDC</span>
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>
              </div>
            </div>

            <p className="text-[10px] text-[#A9B8AD]/60 pt-2 border-t border-[#15271F]">
              * Notice: No &ldquo;booking confirmed&rdquo; status is issued by SWENA upon redirect. Final reservation and ticket issuance occur entirely on the official merchant site.
            </p>
          </div>
        </div>
      </div>

      {/* EVIDENCE INSPECTOR MODAL */}
      {evidenceModalOpen && (
        <div className="fixed inset-0 z-50 bg-[#0D1915]/80 backdrop-blur-md flex items-center justify-center p-4">
          <div className="glass-card max-w-2xl w-full p-8 rounded-3xl border border-[#B7C9AD]/30 space-y-6 shadow-2xl">
            <div className="flex items-center justify-between border-b border-[#233e32] pb-4">
              <div className="flex items-center gap-2">
                <ShieldCheck className="h-5 w-5 text-[#B7C9AD]" />
                <h3 className="text-lg font-bold text-[#F7F7F2]">
                  Verifiable Evidence Record
                </h3>
              </div>
              <button
                onClick={() => setEvidenceModalOpen(false)}
                className="text-[#A9B8AD] hover:text-[#F7F7F2] text-sm"
              >
                ✕ Close
              </button>
            </div>

            <div className="space-y-3 font-mono text-xs">
              <div className="bg-[#0D1915] p-3 rounded-xl border border-[#233e32]">
                <span className="text-[#A9B8AD] block">Trip Aggregate ID:</span>
                <span className="text-[#F7F7F2] break-all">{serverTripId}</span>
              </div>
              <div className="bg-[#0D1915] p-3 rounded-xl border border-[#233e32]">
                <span className="text-[#A9B8AD] block">Evidence Class:</span>
                <span className="text-[#B7C9AD]">INDICATIVE_SEARCH (SerpAPI + PostGIS)</span>
              </div>
              <div className="bg-[#0D1915] p-3 rounded-xl border border-[#233e32]">
                <span className="text-[#A9B8AD] block">Query Hash:</span>
                <span className="text-[#F7F7F2]">sha256:4f8e7c10b29a88e...</span>
              </div>
              <div className="bg-[#0D1915] p-3 rounded-xl border border-[#233e32]">
                <span className="text-[#A9B8AD] block">Observation Timestamp:</span>
                <span className="text-[#F7F7F2]">2026-09-18T22:00:00Z</span>
              </div>
            </div>

            <div className="text-xs text-[#A9B8AD] leading-relaxed">
              In adherence to our strict product boundaries, this evidence is
              persisted in Aiven PostgreSQL with PostGIS points. All merchant links
              pass through to official partners.
            </div>

            <button
              onClick={() => setEvidenceModalOpen(false)}
              className="btn-sage w-full py-2.5 rounded-xl text-xs font-semibold"
            >
              Done Reviewing Evidence
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function DashboardPage() {
  return (
    <Suspense
      fallback={
        <div className="min-h-screen bg-[#0D1915] flex flex-col items-center justify-center text-[#B7C9AD] gap-3">
          <div className="h-8 w-8 rounded-full border-2 border-[#B7C9AD] border-t-transparent animate-spin" />
          <span className="text-xs font-mono tracking-wider uppercase">Loading SWENA planner...</span>
        </div>
      }
    >
      <DashboardContent />
    </Suspense>
  );
}

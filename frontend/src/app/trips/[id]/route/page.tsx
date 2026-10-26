"use client";

import React, { useState, use } from "react";
import Link from "next/link";
import { Navbar } from "@/components/layout/Navbar";
import { Footer } from "@/components/layout/Footer";
import CorridorMapWrapper from "@/components/map/CorridorMapWrapper";
import { useRouteOptimization } from "@/lib/useRouteOptimization";
import type { StopFeatureProperties } from "@/types/routing";
import {
  Compass,
  ArrowLeft,
  Navigation,
  RefreshCw,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Fuel,
  MapPin,
  History,
  Check,
  RotateCcw,
} from "lucide-react";

interface RouteOptimizationPageProps {
  params: Promise<{ id: string }>;
}

export default function RouteOptimizationPage({ params }: RouteOptimizationPageProps) {
  const resolvedParams = use(params);
  const tripId = resolvedParams.id;

  const {
    jobId,
    routeId,
    routeMap,
    versions,
    jobStatus,
    error,
    isSubmitting,
    isPolling,
    isReplanning,
    submitRouteOptimization,
    submitReplan,
    selectVersion,
    startPolling,
    refetchMap,
  } = useRouteOptimization({ autoRecover: true });

  // Replan form state
  const [replanLat, setReplanLat] = useState("12.3150");
  const [replanLng, setReplanLng] = useState("76.6450");
  const [completedStops, setCompletedStops] = useState<string[]>([]);
  const [replanSuccessMessage, setReplanSuccessMessage] = useState<string | null>(null);

  // Extract stops from routeMap if available
  const activeStops = (routeMap?.features || [])
    .filter((f) => f.properties?.feature_type === "stop")
    .map((f) => f.properties as StopFeatureProperties)
    .sort((a, b) => a.stop_order - b.stop_order);

  // Handle GPS location query
  const handleUseDeviceLocation = () => {
    if (typeof navigator !== "undefined" && navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          setReplanLat(pos.coords.latitude.toFixed(6));
          setReplanLng(pos.coords.longitude.toFixed(6));
        },
        (err) => {
          console.warn("Geolocation query denied or failed:", err);
          alert("Could not access device GPS. Please enter coordinates manually.");
        }
      );
    }
  };

  // Toggle completed stop in replan form
  const toggleStopCompleted = (stopId: string) => {
    setCompletedStops((prev) =>
      prev.includes(stopId) ? prev.filter((id) => id !== stopId) : [...prev, stopId]
    );
  };

  // Handle replan submission
  const handleReplanSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setReplanSuccessMessage(null);

    const lat = parseFloat(replanLat);
    const lng = parseFloat(replanLng);

    if (isNaN(lat) || isNaN(lng)) {
      alert("Please provide valid latitude and longitude coordinates.");
      return;
    }

    const newJobId = await submitReplan({
      current_location: {
        latitude: lat,
        longitude: lng,
      },
      completed_stop_ids: completedStops,
    });

    if (newJobId) {
      setReplanSuccessMessage(`Replan initiated. Tracking job ${newJobId}...`);
    }
  };

  // Handle sample route submission if no route or job exists yet
  const handleSampleRouteSubmit = async () => {
    await submitRouteOptimization({
      owner_id: "00000000-0000-0000-0000-000000000001",
      trip_id: tripId,
      transport_mode: "CAR_PETROL",
      departure_at: new Date().toISOString(),
      stops: [
        {
          name: "Bengaluru MG Road Origin",
          location: { latitude: 12.9716, longitude: 77.5946 },
          stop_type: "ORIGIN",
          dwell_seconds: 0,
          is_must_visit: true,
        },
        {
          name: "Mysuru Palace Heritage",
          location: { latitude: 12.3051, longitude: 76.6551 },
          stop_type: "DESTINATION",
          dwell_seconds: 3600,
          is_must_visit: true,
        },
        {
          name: "Madikeri Coorg Viewpoint",
          location: { latitude: 12.4244, longitude: 75.7382 },
          stop_type: "DESTINATION",
          dwell_seconds: 7200,
          is_must_visit: true,
        },
      ],
    });
  };

  const routeStatus = routeMap?.properties?.status || routeMap?.status;
  const totalDistKm = routeMap?.properties?.total_distance_m
    ? (routeMap.properties.total_distance_m / 1000).toFixed(1)
    : routeMap?.total_distance_m
      ? (routeMap.total_distance_m / 1000).toFixed(1)
      : null;

  const totalDurationStr = (() => {
    const s =
      routeMap?.properties?.total_travel_seconds ??
      routeMap?.total_travel_seconds;
    if (s === undefined) return null;
    const hours = Math.floor(s / 3600);
    const mins = Math.round((s % 3600) / 60);
    return hours > 0 ? `${hours}h ${mins}m` : `${mins}m`;
  })();

  const fuelCost = routeMap?.properties?.fuel_cost ?? routeMap?.fuel_cost;
  const diagnostics =
    routeMap?.properties?.diagnostics || routeMap?.diagnostics || [];
  const currentVersion =
    routeMap?.properties?.route_version ?? routeMap?.route_version;

  return (
    <div className="min-h-screen bg-[#0A1410] text-[#F7F7F2] flex flex-col font-sans">
      <Navbar />

      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Breadcrumb Navigation & Header */}
        <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
          <div>
            <Link
              href={`/trips/${tripId}`}
              className="inline-flex items-center gap-1.5 text-xs text-[#A9B8AD] hover:text-[#B7C9AD] transition-colors mb-2"
            >
              <ArrowLeft className="w-3.5 h-3.5" />
              <span>Back to Trip Details</span>
            </Link>
            <h1 className="text-2xl sm:text-3xl font-extrabold tracking-tight text-[#F7F7F2] flex items-center gap-2.5">
              <Compass className="w-7 h-7 text-[#B7C9AD]" />
              <span>Route Optimizer & Geospatial Map</span>
            </h1>
          </div>

          {/* Version Selector Dropdown */}
          {versions.length > 0 && (
            <div className="flex items-center gap-2 bg-[#15271F] px-3 py-1.5 rounded-xl border border-[#233E32]">
              <History className="w-4 h-4 text-[#B7C9AD]" />
              <label htmlFor="version-select" className="text-xs text-[#A9B8AD]">
                Version:
              </label>
              <select
                id="version-select"
                className="bg-[#0D1915] text-xs font-semibold text-[#F7F7F2] border border-[#233E32] rounded-lg px-2 py-1 focus:outline-none focus:border-[#B7C9AD]"
                value={routeId || ""}
                onChange={(e) => selectVersion(e.target.value)}
              >
                {versions.map((v) => (
                  <option key={v.route_id} value={v.route_id}>
                    v{v.route_version} (
                    {v.distance_delta_m !== null && v.distance_delta_m !== 0
                      ? `${v.distance_delta_m > 0 ? "+" : ""}${(
                          v.distance_delta_m / 1000
                        ).toFixed(1)}km`
                      : "Base"}
                    ) • {v.status}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>

        {/* Polling / Solving Progress Banner */}
        {isPolling && (
          <div className="mb-6 bg-[#15271F] border border-[rgba(183,201,173,0.3)] rounded-2xl p-4 flex items-center gap-4 shadow-xl animate-pulse">
            <div className="w-6 h-6 rounded-full border-2 border-[#B7C9AD] border-t-transparent animate-spin shrink-0" />
            <div className="flex-1 min-w-0">
              <div className="text-sm font-semibold text-[#F7F7F2] flex items-center gap-2">
                <span>Solving Vehicle Routing Problem with Time Windows...</span>
                <span className="text-[10px] uppercase tracking-wider bg-[#102D25] text-[#B7C9AD] px-2 py-0.5 rounded-full border border-[#233E32]">
                  {jobStatus}
                </span>
              </div>
              <p className="text-xs text-[#A9B8AD] truncate">
                Job ID: {jobId} • Polling Valhalla road matrix and OR-Tools state machine.
              </p>
            </div>
          </div>
        )}

        {/* Error Card with Retry Button */}
        {error && (
          <div className="mb-6 bg-red-950/80 border border-red-700/80 rounded-2xl p-4 flex items-start justify-between gap-4 shadow-xl">
            <div className="flex items-start gap-3">
              <AlertTriangle className="w-5 h-5 text-red-400 shrink-0 mt-0.5" />
              <div>
                <div className="text-sm font-bold text-red-200">Optimization Error</div>
                <div className="text-xs text-red-300 mt-1 leading-relaxed">{error}</div>
              </div>
            </div>
            <button
              onClick={() => (jobId ? startPolling(jobId) : handleSampleRouteSubmit())}
              className="px-3 py-1.5 bg-red-900/60 hover:bg-red-800/60 border border-red-600/50 rounded-lg text-xs font-semibold text-red-100 flex items-center gap-1.5 transition-colors cursor-pointer"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              <span>Retry</span>
            </button>
          </div>
        )}

        {/* Replan Success Flash Message */}
        {replanSuccessMessage && (
          <div className="mb-6 bg-emerald-950/80 border border-emerald-700/80 rounded-2xl p-4 flex items-center gap-3 text-xs text-emerald-200 shadow-xl">
            <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
            <span>{replanSuccessMessage}</span>
          </div>
        )}

        {/* Main Grid: Left Map Canvas, Right Summary and Replan Controls */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-10">
          {/* Map Column (2 Span on desktop) */}
          <div className="lg:col-span-2 space-y-4">
            <div className="h-130 w-full rounded-2xl overflow-hidden border border-[#233E32] shadow-2xl relative">
              <CorridorMapWrapper geoJson={routeMap} />
            </div>

            {/* Diagnostics Banner if INFEASIBLE or Warnings */}
            {diagnostics.length > 0 && (
              <div
                className={`p-4 rounded-xl border ${
                  routeStatus === "INFEASIBLE"
                    ? "bg-red-950/60 border-red-800/80 text-red-200"
                    : "bg-amber-950/60 border-amber-800/80 text-amber-200"
                }`}
              >
                <div className="flex items-center gap-2 font-bold text-xs uppercase tracking-wider mb-1.5">
                  <AlertTriangle className="w-4 h-4" />
                  <span>
                    {routeStatus === "INFEASIBLE"
                      ? "Mathematical Hard Constraint Infeasibility"
                      : "Route Optimization Advisory"}
                  </span>
                </div>
                <ul className="text-xs space-y-1 pl-5 list-disc">
                  {diagnostics.map((diag, i) => (
                    <li key={i}>{diag}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          {/* Right Column: Trip Summary Card & Mid-Trip Replan Panel */}
          <div className="space-y-6">
            {/* Trip Summary Card */}
            <div className="bg-[#15271F] rounded-2xl border border-[#233E32] p-5 shadow-xl">
              <h2 className="text-sm font-bold text-[#F7F7F2] uppercase tracking-wider mb-4 flex items-center justify-between">
                <span>Trip Execution Metrics</span>
                {routeStatus && (
                  <span
                    className={`px-2.5 py-0.5 rounded-full text-[10px] font-extrabold border ${
                      routeStatus === "FEASIBLE" || routeStatus === "OPTIMAL"
                        ? "bg-emerald-950 text-emerald-300 border-emerald-700"
                        : routeStatus === "PARTIAL"
                          ? "bg-amber-950 text-amber-300 border-amber-700"
                          : "bg-red-950 text-red-300 border-red-700"
                    }`}
                  >
                    {routeStatus} (v{currentVersion || 1})
                  </span>
                )}
              </h2>

              <div className="grid grid-cols-2 gap-3 mb-4">
                <div className="bg-[#0D1915] p-3 rounded-xl border border-[#233E32]">
                  <div className="text-[10px] text-[#A9B8AD] flex items-center gap-1 mb-1">
                    <Navigation className="w-3 h-3 text-[#B7C9AD]" />
                    <span>Distance</span>
                  </div>
                  <div className="text-base font-bold text-[#F7F7F2]">
                    {totalDistKm ? `${totalDistKm} km` : "N/A"}
                  </div>
                </div>

                <div className="bg-[#0D1915] p-3 rounded-xl border border-[#233E32]">
                  <div className="text-[10px] text-[#A9B8AD] flex items-center gap-1 mb-1">
                    <Clock className="w-3 h-3 text-[#B7C9AD]" />
                    <span>Duration</span>
                  </div>
                  <div className="text-base font-bold text-[#F7F7F2]">
                    {totalDurationStr || "N/A"}
                  </div>
                </div>

                <div className="bg-[#0D1915] p-3 rounded-xl border border-[#233E32] col-span-2">
                  <div className="text-[10px] text-[#A9B8AD] flex items-center gap-1 mb-1">
                    <Fuel className="w-3 h-3 text-[#B7C9AD]" />
                    <span>Fuel Estimate</span>
                  </div>
                  <div className="text-base font-bold text-[#B7C9AD]">
                    {fuelCost ? `₹${fuelCost}` : "Included in Brief"}
                  </div>
                </div>
              </div>

              {!routeMap && !isPolling && (
                <button
                  onClick={handleSampleRouteSubmit}
                  disabled={isSubmitting}
                  className="w-full py-2.5 px-4 bg-[#B7C9AD] hover:bg-[#a5b99a] text-[#102D25] font-bold text-xs rounded-xl transition-all shadow-md flex items-center justify-center gap-2 cursor-pointer disabled:opacity-50"
                >
                  <Navigation className="w-4 h-4" />
                  <span>{isSubmitting ? "Submitting..." : "Generate Optimal Route"}</span>
                </button>
              )}
            </div>

            {/* Mid-Trip Replan Panel */}
            <div className="bg-[#15271F] rounded-2xl border border-[#233E32] p-5 shadow-xl">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-bold text-[#F7F7F2] uppercase tracking-wider flex items-center gap-2">
                  <RefreshCw className="w-4 h-4 text-[#B7C9AD]" />
                  <span>Mid-Trip Dynamic Replan</span>
                </h2>
                <button
                  type="button"
                  onClick={handleUseDeviceLocation}
                  className="text-[10px] text-[#B7C9AD] hover:underline flex items-center gap-1 cursor-pointer"
                >
                  <MapPin className="w-3 h-3" />
                  <span>Use Device GPS</span>
                </button>
              </div>

              <p className="text-[11px] text-[#A9B8AD] mb-4">
                Encountered a road closure or schedule delay? Submit your current GPS position
                and check off completed stops to recalculate an updated detour route.
              </p>

              <form onSubmit={handleReplanSubmit} className="space-y-4">
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <label className="block text-[10px] text-[#A9B8AD] mb-1 font-semibold">
                      Current Lat
                    </label>
                    <input
                      type="text"
                      value={replanLat}
                      onChange={(e) => setReplanLat(e.target.value)}
                      className="w-full bg-[#0D1915] border border-[#233E32] rounded-lg px-2.5 py-1.5 text-xs text-[#F7F7F2] focus:outline-none focus:border-[#B7C9AD]"
                    />
                  </div>
                  <div>
                    <label className="block text-[10px] text-[#A9B8AD] mb-1 font-semibold">
                      Current Lng
                    </label>
                    <input
                      type="text"
                      value={replanLng}
                      onChange={(e) => setReplanLng(e.target.value)}
                      className="w-full bg-[#0D1915] border border-[#233E32] rounded-lg px-2.5 py-1.5 text-xs text-[#F7F7F2] focus:outline-none focus:border-[#B7C9AD]"
                    />
                  </div>
                </div>

                {/* Preset Location Chips */}
                <div className="flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    onClick={() => {
                      setReplanLat("12.3150");
                      setReplanLng("76.6450");
                    }}
                    className="text-[9px] px-2 py-0.5 rounded-full bg-[#0D1915] text-[#A9B8AD] border border-[#233E32] hover:border-[#B7C9AD] cursor-pointer"
                  >
                    Mysuru Outer Bypass
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setReplanLat("12.4500");
                      setReplanLng("75.9500");
                    }}
                    className="text-[9px] px-2 py-0.5 rounded-full bg-[#0D1915] text-[#A9B8AD] border border-[#233E32] hover:border-[#B7C9AD] cursor-pointer"
                  >
                    Kushalnagar Entry
                  </button>
                </div>

                {/* Completed Stops Checklist */}
                {activeStops.length > 0 && (
                  <div>
                    <label className="block text-[10px] text-[#A9B8AD] mb-2 font-semibold">
                      Freeze Completed Stops:
                    </label>
                    <div className="space-y-1.5 max-h-40 overflow-y-auto pr-1">
                      {activeStops.map((stop) => {
                        const isDone = completedStops.includes(stop.id);
                        return (
                          <div
                            key={stop.id}
                            onClick={() => toggleStopCompleted(stop.id)}
                            className={`p-2 rounded-lg border text-xs flex items-center justify-between cursor-pointer transition-colors ${
                              isDone
                                ? "bg-[#102D25] border-[#B7C9AD]/50 text-[#F7F7F2]"
                                : "bg-[#0D1915] border-[#233E32] text-[#A9B8AD] hover:border-[#A9B8AD]/40"
                            }`}
                          >
                            <span className="truncate mr-2">
                              {stop.stop_order}. {stop.name || "Stop"}
                            </span>
                            <div
                              className={`w-4 h-4 rounded flex items-center justify-center border ${
                                isDone
                                  ? "bg-[#B7C9AD] border-[#B7C9AD] text-[#102D25]"
                                  : "border-[#233E32]"
                              }`}
                            >
                              {isDone && <Check className="w-3 h-3 stroke-3" />}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                <button
                  type="submit"
                  disabled={isReplanning || !routeId}
                  className="w-full py-2.5 px-4 bg-[#102D25] hover:bg-[#1a382f] text-[#B7C9AD] border border-[#B7C9AD]/50 font-bold text-xs rounded-xl transition-all shadow-md flex items-center justify-center gap-2 cursor-pointer disabled:opacity-40"
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${isReplanning ? "animate-spin" : ""}`} />
                  <span>{isReplanning ? "Recalculating..." : "Replan & Detour"}</span>
                </button>
              </form>
            </div>
          </div>
        </div>
      </main>

      <Footer />
    </div>
  );
}

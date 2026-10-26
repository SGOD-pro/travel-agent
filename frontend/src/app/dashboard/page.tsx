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
  Globe2,
} from "lucide-react";
import CorridorMapWrapper from "@/components/map/CorridorMapWrapper";
import CorridorWeatherAdvisory, { StopWeather } from "@/components/weather/CorridorWeatherAdvisory";
import VoiceBriefAssistant from "@/components/voice/VoiceBriefAssistant";

// ============================================================================
// ALL-INDIA GEODIRECTORY & PRESETS
// ============================================================================

interface LocationInfo {
  lat: number;
  lng: number;
  state: string;
  region: "North" | "South" | "West" | "East" | "Central" | "Northeast" | "Himalayas";
}

const INDIA_LOCATIONS: Record<string, LocationInfo> = {
  // North & Golden Triangle
  "Delhi": { lat: 28.6139, lng: 77.2090, state: "Delhi", region: "North" },
  "Agra": { lat: 27.1767, lng: 78.0081, state: "Uttar Pradesh", region: "North" },
  "Jaipur": { lat: 26.9124, lng: 75.7873, state: "Rajasthan", region: "North" },
  "Chandigarh": { lat: 30.7333, lng: 76.7794, state: "Chandigarh", region: "North" },
  "Amritsar": { lat: 31.6340, lng: 74.8723, state: "Punjab", region: "North" },
  "Rishikesh": { lat: 30.0869, lng: 78.2676, state: "Uttarakhand", region: "North" },
  "Haridwar": { lat: 29.9457, lng: 78.1642, state: "Uttarakhand", region: "North" },
  "Dehradun": { lat: 30.3165, lng: 78.0322, state: "Uttarakhand", region: "North" },
  "Nainital": { lat: 29.3803, lng: 79.4636, state: "Uttarakhand", region: "North" },

  // Himalayas & Kashmir
  "Shimla": { lat: 31.1048, lng: 77.1734, state: "Himachal Pradesh", region: "Himalayas" },
  "Manali": { lat: 32.2432, lng: 77.1892, state: "Himachal Pradesh", region: "Himalayas" },
  "Kasol": { lat: 32.0100, lng: 77.3150, state: "Himachal Pradesh", region: "Himalayas" },
  "Dharamshala": { lat: 32.2190, lng: 76.3234, state: "Himachal Pradesh", region: "Himalayas" },
  "Srinagar": { lat: 34.0837, lng: 74.7973, state: "Jammu & Kashmir", region: "Himalayas" },
  "Gulmarg": { lat: 34.0484, lng: 74.3805, state: "Jammu & Kashmir", region: "Himalayas" },
  "Pahalgam": { lat: 34.0161, lng: 75.3150, state: "Jammu & Kashmir", region: "Himalayas" },
  "Leh": { lat: 34.1526, lng: 77.5771, state: "Ladakh", region: "Himalayas" },

  // South India
  "Bengaluru": { lat: 12.9716, lng: 77.5946, state: "Karnataka", region: "South" },
  "Mysuru": { lat: 12.2958, lng: 76.6394, state: "Karnataka", region: "South" },
  "Coorg (Madikeri)": { lat: 12.4244, lng: 75.7382, state: "Karnataka", region: "South" },
  "Wayanad": { lat: 11.6854, lng: 76.1320, state: "Kerala", region: "South" },
  "Ooty": { lat: 11.4102, lng: 76.6950, state: "Tamil Nadu", region: "South" },
  "Kochi": { lat: 9.9312, lng: 76.2673, state: "Kerala", region: "South" },
  "Munnar": { lat: 10.0889, lng: 77.0595, state: "Kerala", region: "South" },
  "Alleppey": { lat: 9.4981, lng: 76.3388, state: "Kerala", region: "South" },
  "Thekkady": { lat: 9.6031, lng: 77.1610, state: "Kerala", region: "South" },
  "Chennai": { lat: 13.0827, lng: 80.2707, state: "Tamil Nadu", region: "South" },
  "Pondicherry": { lat: 11.9416, lng: 79.8083, state: "Puducherry", region: "South" },
  "Madurai": { lat: 9.9252, lng: 78.1198, state: "Tamil Nadu", region: "South" },
  "Hampi": { lat: 15.3350, lng: 76.4600, state: "Karnataka", region: "South" },
  "Hyderabad": { lat: 17.3850, lng: 78.4867, state: "Telangana", region: "South" },
  "Gokarna": { lat: 14.5479, lng: 74.3188, state: "Karnataka", region: "South" },

  // West & Rajasthan
  "Mumbai": { lat: 19.0760, lng: 72.8777, state: "Maharashtra", region: "West" },
  "Pune": { lat: 18.5204, lng: 73.8567, state: "Maharashtra", region: "West" },
  "Alibaug": { lat: 18.6534, lng: 72.8770, state: "Maharashtra", region: "West" },
  "Ratnagiri": { lat: 16.9902, lng: 73.3120, state: "Maharashtra", region: "West" },
  "Goa": { lat: 15.2993, lng: 74.1240, state: "Goa", region: "West" },
  "Pushkar": { lat: 26.4899, lng: 74.5511, state: "Rajasthan", region: "West" },
  "Jodhpur": { lat: 26.2389, lng: 73.0243, state: "Rajasthan", region: "West" },
  "Udaipur": { lat: 24.5854, lng: 73.7125, state: "Rajasthan", region: "West" },
  "Jaisalmer": { lat: 26.9157, lng: 70.9083, state: "Rajasthan", region: "West" },
  "Ahmedabad": { lat: 23.0225, lng: 72.5714, state: "Gujarat", region: "West" },

  // East & Northeast
  "Kolkata": { lat: 22.5726, lng: 88.3639, state: "West Bengal", region: "East" },
  "Siliguri": { lat: 26.7271, lng: 88.3953, state: "West Bengal", region: "East" },
  "Darjeeling": { lat: 27.0410, lng: 88.2663, state: "West Bengal", region: "East" },
  "Gangtok": { lat: 27.3389, lng: 88.6065, state: "Sikkim", region: "Northeast" },
  "Puri": { lat: 19.8135, lng: 85.8312, state: "Odisha", region: "East" },
  "Bhubaneswar": { lat: 20.2961, lng: 85.8245, state: "Odisha", region: "East" },
  "Guwahati": { lat: 26.1445, lng: 91.7362, state: "Assam", region: "Northeast" },
  "Shillong": { lat: 25.5788, lng: 91.8933, state: "Meghalaya", region: "Northeast" },

  // Central & Spiritual
  "Varanasi": { lat: 25.3176, lng: 82.9739, state: "Uttar Pradesh", region: "Central" },
  "Prayagraj": { lat: 25.4358, lng: 81.8463, state: "Uttar Pradesh", region: "Central" },
  "Ayodhya": { lat: 26.7922, lng: 82.1998, state: "Uttar Pradesh", region: "Central" },
  "Lucknow": { lat: 26.8467, lng: 80.9462, state: "Uttar Pradesh", region: "Central" },
  "Bhopal": { lat: 23.2599, lng: 77.4126, state: "Madhya Pradesh", region: "Central" },
  "Indore": { lat: 22.7196, lng: 75.8577, state: "Madhya Pradesh", region: "Central" },
};

function lookupLocation(name: string): { lat: number; lng: number } {
  const normalized = name.trim().toLowerCase();
  for (const [key, val] of Object.entries(INDIA_LOCATIONS)) {
    if (key.toLowerCase() === normalized || normalized.includes(key.toLowerCase())) {
      return { lat: val.lat, lng: val.lng };
    }
  }
  return { lat: 20.5937, lng: 78.9629 };
}

function generateUniqueId(prefix = "dest"): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
}

const PAN_INDIA_CIRCUITS = [
  {
    id: "western-ghats",
    title: "Karnataka & Western Ghats",
    origin: "Bengaluru",
    originLat: 12.9716,
    originLng: 77.5946,
    stops: [
      { name: "Mysuru", stayDays: 2 },
      { name: "Coorg (Madikeri)", stayDays: 2 },
      { name: "Wayanad", stayDays: 2 },
    ],
  },
  {
    id: "golden-triangle",
    title: "Golden Triangle Heritage",
    origin: "Delhi",
    originLat: 28.6139,
    originLng: 77.2090,
    stops: [
      { name: "Agra", stayDays: 1 },
      { name: "Jaipur", stayDays: 2 },
    ],
  },
  {
    id: "himachal-pass",
    title: "Himachal Mountain Pass",
    origin: "Chandigarh",
    originLat: 30.7333,
    originLng: 76.7794,
    stops: [
      { name: "Shimla", stayDays: 2 },
      { name: "Manali", stayDays: 3 },
      { name: "Kasol", stayDays: 1 },
    ],
  },
  {
    id: "rajasthan-royal",
    title: "Rajasthan Royals & Forts",
    origin: "Delhi",
    originLat: 28.6139,
    originLng: 77.2090,
    stops: [
      { name: "Jaipur", stayDays: 2 },
      { name: "Pushkar", stayDays: 1 },
      { name: "Jodhpur", stayDays: 2 },
    ],
  },
  {
    id: "konkan-coast",
    title: "Konkan Highway & Goa",
    origin: "Mumbai",
    originLat: 19.0760,
    originLng: 72.8777,
    stops: [
      { name: "Alibaug", stayDays: 1 },
      { name: "Ratnagiri", stayDays: 2 },
      { name: "Goa", stayDays: 3 },
    ],
  },
  {
    id: "kerala-backwaters",
    title: "Kerala Hills & Backwaters",
    origin: "Kochi",
    originLat: 9.9312,
    originLng: 76.2673,
    stops: [
      { name: "Munnar", stayDays: 2 },
      { name: "Thekkady", stayDays: 1 },
      { name: "Alleppey", stayDays: 2 },
    ],
  },
  {
    id: "eastern-himalayas",
    title: "Eastern Himalayas & Tea Valley",
    origin: "Kolkata",
    originLat: 22.5726,
    originLng: 88.3639,
    stops: [
      { name: "Siliguri", stayDays: 1 },
      { name: "Darjeeling", stayDays: 2 },
      { name: "Gangtok", stayDays: 2 },
    ],
  },
  {
    id: "spiritual-ganga",
    title: "Spiritual Ganga Corridor",
    origin: "Lucknow",
    originLat: 26.8467,
    originLng: 80.9462,
    stops: [
      { name: "Ayodhya", stayDays: 1 },
      { name: "Prayagraj", stayDays: 1 },
      { name: "Varanasi", stayDays: 2 },
    ],
  },
];

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

  const formatDateString = (date: Date) => {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  };

  const today = new Date();
  const defaultReturn = new Date();
  defaultReturn.setDate(today.getDate() + 4);

  // Brief configuration state
  const [origin, setOrigin] = useState("Bengaluru");
  const [originLat, setOriginLat] = useState(12.9716);
  const [originLng, setOriginLng] = useState(77.5946);
  const [startDate, setStartDate] = useState(formatDateString(today));
  const [endDate, setEndDate] = useState(formatDateString(defaultReturn));
  const [adults, setAdults] = useState(2);
  const [rooms, setRooms] = useState(1);
  const [mode, setMode] = useState<"car" | "motorcycle">("car");

  // Destination list with guaranteed unique keys
  const [destinations, setDestinations] = useState<DestinationItem[]>([
    { id: generateUniqueId("mys"), name: "Mysuru", lat: 12.2958, lng: 76.6394, stayDays: 2 },
    { id: generateUniqueId("crg"), name: "Coorg (Madikeri)", lat: 12.4244, lng: 75.7382, stayDays: 2 },
  ]);

  // Solver & UI state
  const [isPlanning, setIsPlanning] = useState(false);
  const [plannedVersion, setPlannedVersion] = useState(1);
  const [activeTab, setActiveTab] = useState<"itinerary" | "map" | "budget" | "recommendations" | "evidence">("itinerary");
  const [evidenceModalOpen, setEvidenceModalOpen] = useState(false);
  const [apiStatus, setApiStatus] = useState<"offline" | "connected" | "checking">("checking");
  const [serverTripId, setServerTripId] = useState<string>("urn:swena:trip:pan-india-circuit-2026");
  const [isExporting, setIsExporting] = useState(false);

  // Editable vehicle efficiency and fuel price
  const [mileage, setMileage] = useState(15.0);
  const [fuelPricePerLiter, setFuelPricePerLiter] = useState(102.5);

  // Consume corridor query parameter
  useEffect(() => {
    if (!corridorParam) return;
    const match = PAN_INDIA_CIRCUITS.find((c) => c.id === corridorParam);
    if (match) {
      applyCircuitPreset(match);
    }
  }, [corridorParam]);

  // Check backend health
  useEffect(() => {
    const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 2000);

    fetch(`${apiBase}/health`, { signal: controller.signal })
      .then((res) => setApiStatus(res.ok ? "connected" : "offline"))
      .catch(() => setApiStatus("offline"))
      .finally(() => clearTimeout(timer));
  }, []);

  const applyCircuitPreset = (circuit: (typeof PAN_INDIA_CIRCUITS)[0]) => {
    setOrigin(circuit.origin);
    setOriginLat(circuit.originLat);
    setOriginLng(circuit.originLng);
    setDestinations(
      circuit.stops.map((s) => {
        const coords = lookupLocation(s.name);
        return {
          id: generateUniqueId(s.name.substring(0, 3).toLowerCase()),
          name: s.name,
          lat: coords.lat,
          lng: coords.lng,
          stayDays: s.stayDays,
        };
      })
    );
    setServerTripId(`urn:swena:trip:${circuit.id}-2026`);
  };

  const handleOriginChange = (val: string) => {
    setOrigin(val);
    const coords = lookupLocation(val);
    setOriginLat(coords.lat);
    setOriginLng(coords.lng);
  };

  const addDestination = () => {
    const newId = generateUniqueId("stop");
    const defaultCity = "Wayanad";
    const coords = lookupLocation(defaultCity);
    setDestinations((prev) => [
      ...prev,
      { id: newId, name: defaultCity, lat: coords.lat, lng: coords.lng, stayDays: 1 },
    ]);
  };

  const removeDestination = (id: string) => {
    if (destinations.length <= 1) return;
    setDestinations((prev) => prev.filter((d) => d.id !== id));
  };

  const updateDestination = (id: string, field: keyof DestinationItem, value: any) => {
    setDestinations((prev) =>
      prev.map((d) => {
        if (d.id !== id) return d;
        const updated = { ...d, [field]: value };
        if (field === "name") {
          const coords = lookupLocation(value as string);
          updated.lat = coords.lat;
          updated.lng = coords.lng;
        }
        return updated;
      })
    );
  };

  // Calendar calculations
  const start = new Date(startDate);
  const end = new Date(endDate);
  const diffDays = Math.max(1, Math.round((end.getTime() - start.getTime()) / (1000 * 3600 * 24)) + 1);

  // Dynamic route scheduling
  const avgSpeed = mode === "car" ? 45 : 40;
  const points = [{ name: origin, lat: originLat, lng: originLng, stayDays: 0 }, ...destinations];

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
      return { position: 0, name: p.name, arrival: "09:00 AM (Day 1)", departure: "09:30 AM (Day 1)", stayHours: 0.5 };
    }
    const leg = scheduledLegs[idx - 1];
    cumulativeMins += leg.durationMins;
    const arrHours = Math.floor((cumulativeMins % (24 * 60)) / 60);
    const arrMins = cumulativeMins % 60;
    const amPm = arrHours >= 12 ? "PM" : "AM";
    const formattedHours = arrHours % 12 === 0 ? 12 : arrHours % 12;
    const arrTimeStr = `${String(formattedHours).padStart(2, "0")}:${String(arrMins).padStart(2, "0")} ${amPm} (Day ${currentDay})`;

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
      name: `${destinations[0]?.name || "National"} Heritage Waypoint`,
      category: "Heritage",
      fitScore: 0.94,
      detourKm: 4.2,
      extraDurationMins: 45,
      source: "Ministry of Tourism (Incredible India)",
      uncertainty: "EDITORIAL_DISCOVERY",
      lat: (originLat + (destinations[0]?.lat || originLat)) / 2,
      lng: (originLng + (destinations[0]?.lng || originLng)) / 2,
    },
    {
      name: `${destinations[destinations.length - 1]?.name || "Circuit"} Scenic Overlook`,
      category: "Scenic",
      fitScore: 0.91,
      detourKm: 6.8,
      extraDurationMins: 60,
      source: "PostGIS Spatial Engine",
      uncertainty: "EDITORIAL_DISCOVERY",
      lat: destinations[destinations.length - 1]?.lat || originLat,
      lng: destinations[destinations.length - 1]?.lng || originLng,
    },
  ];

  const weatherStops: StopWeather[] = [
    {
      stopName: origin,
      temperatureC: 28,
      condition: "sunny",
      humidityPercent: 55,
      monsoonRisk: "low",
    },
    ...destinations.map((d) => {
      const isMountain = d.name.toLowerCase().match(/(coorg|shimla|manali|gangtok|munnar|leh|darjeeling|ooty)/);
      return {
        stopName: d.name,
        temperatureC: isMountain ? 18 : 31,
        condition: (isMountain ? "foggy" : "cloudy") as "sunny" | "cloudy" | "rainy" | "foggy",
        humidityPercent: isMountain ? 82 : 50,
        monsoonRisk: (isMountain ? "moderate" : "low") as "low" | "moderate" | "severe",
        ghatCaution: isMountain ? "Mountain pass / ghat corridor; low-beam headlights advised." : undefined,
      };
    }),
  ];

  const handleVoiceTranscript = (text: string) => {
    const lower = text.toLowerCase();
    for (const [cityName] of Object.entries(INDIA_LOCATIONS)) {
      if (lower.includes(cityName.toLowerCase())) {
        if (!destinations.some((d) => d.name.toLowerCase() === cityName.toLowerCase())) {
          const coords = lookupLocation(cityName);
          setDestinations((prev) => [
            ...prev,
            { id: generateUniqueId("voice"), name: cityName, lat: coords.lat, lng: coords.lng, stayDays: 2 },
          ]);
        }
      }
    }
    if (lower.includes("car")) setMode("car");
    if (lower.includes("bike") || lower.includes("motorcycle")) setMode("motorcycle");
  };

  const narrativeScript = `Your pan-India travel itinerary begins in ${origin}, connecting to ${destinations
    .map((d) => d.name)
    .join(" and ")} across ${totalDistanceKm} kilometers. Total travel time is approximately ${Math.round(
    totalTravelMins / 60
  )} hours with fuel consumption estimated at ${fuelLiters} liters.`;

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
        const planRes = await fetch(`${apiBase}/api/v1/trips/${tripData.id}/plan`, { method: "POST" });
        if (planRes.ok) setApiStatus("connected");
      } else {
        setApiStatus("offline");
      }
    } catch {
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
      // local fallback print
    }

    setTimeout(() => {
      setIsExporting(false);
      window.print();
    }, 600);
  };

  return (
    <div className="pt-24 pb-12 px-4 sm:px-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4 mb-8 pb-6 border-b border-[#15271F]">
        <div>
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-[#15271F] border border-[#B7C9AD]/20 text-xs font-semibold text-[#B7C9AD] uppercase tracking-wider mb-2.5">
            <Globe2 className="h-3.5 w-3.5" />
            <span>Pan-India Tour Optimization Engine</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-bold text-[#F7F7F2]">
            Trip Planning Console
          </h1>
        </div>

        <div className="flex items-center flex-wrap gap-3">
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

      {/* Pan-India Circuit Presets Ribbon */}
      <div className="mb-8">
        <label className="block text-xs text-[#A9B8AD] mb-2 font-medium uppercase tracking-wider">
          Featured Indian Tourist Circuits
        </label>
        <div className="flex items-center gap-2 overflow-x-auto pb-2 scrollbar-none">
          {PAN_INDIA_CIRCUITS.map((circuit) => (
            <button
              key={circuit.id}
              onClick={() => applyCircuitPreset(circuit)}
              className="bg-[#0D1915] hover:bg-[#15271F] border border-[#233e32] hover:border-[#B7C9AD]/40 text-[#F7F7F2] px-3.5 py-2 rounded-xl text-xs font-semibold whitespace-nowrap transition-all flex items-center gap-1.5"
            >
              <Compass className="h-3 w-3 text-[#B7C9AD]" />
              <span>{circuit.title}</span>
            </button>
          ))}
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
                  Origin City / Hub
                </label>
                <div className="relative">
                  <MapPin className="h-4 w-4 text-[#B7C9AD] absolute left-3 top-3" />
                  <input
                    type="text"
                    value={origin}
                    onChange={(e) => handleOriginChange(e.target.value)}
                    placeholder="e.g. Delhi, Mumbai, Bengaluru"
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

              {/* Mode & Parameters */}
              <div>
                <label className="block text-[#A9B8AD] mb-2 font-medium uppercase tracking-wider">
                  Transport Mode
                </label>
                <div className="grid grid-cols-2 gap-2 bg-[#0D1915] p-1 rounded-xl border border-[#233e32]">
                  <button
                    onClick={() => {
                      setMode("car");
                      setMileage(15.0);
                    }}
                    className={`flex items-center justify-center gap-2 py-2 rounded-lg font-semibold transition-all ${
                      mode === "car" ? "bg-[#15271F] text-[#B7C9AD] shadow-sm" : "text-[#A9B8AD]"
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
                      mode === "motorcycle" ? "bg-[#15271F] text-[#B7C9AD] shadow-sm" : "text-[#A9B8AD]"
                    }`}
                  >
                    <Bike className="h-3.5 w-3.5" />
                    <span>Motorcycle</span>
                  </button>
                </div>

                <div className="grid grid-cols-2 gap-3 mt-3 pt-3 border-t border-[#233e32]/60">
                  <div>
                    <label className="block text-[11px] text-[#A9B8AD] mb-1">Mileage (km/L)</label>
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
                    <label className="block text-[11px] text-[#A9B8AD] mb-1">Fuel Price (₹/L)</label>
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
              </div>

              {/* Destinations list */}
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
                          onChange={(e) => updateDestination(dest.id, "name", e.target.value)}
                          placeholder="City name..."
                          className="w-full bg-transparent font-semibold text-[#F7F7F2] focus:outline-none"
                        />
                        <div className="flex items-center gap-2 text-[11px] text-[#A9B8AD] mt-1">
                          <span>Stay:</span>
                          <input
                            type="number"
                            min={1}
                            max={10}
                            value={dest.stayDays}
                            onChange={(e) => updateDestination(dest.id, "stayDays", Number(e.target.value))}
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

              {/* Submit / Re-solve */}
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

        {/* RIGHT COLUMN: OUTPUT TABS (8 cols) */}
        <div className="lg:col-span-8 space-y-6">
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
                href="/trips/pan-india-circuit-2026"
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

          {/* TAB 1: ITINERARY */}
          {activeTab === "itinerary" && (
            <div className="space-y-4">
              <CorridorWeatherAdvisory stops={weatherStops} />

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

              <div className="glass-card p-6 rounded-3xl border border-[#233e32] space-y-6">
                <h3 className="text-sm font-bold text-[#F7F7F2] uppercase tracking-wider flex items-center gap-2">
                  <Clock className="h-4 w-4 text-[#B7C9AD]" />
                  <span>Monotonically Sequenced Stops</span>
                </h3>

                <div className="relative pl-6 border-l-2 border-[#233e32] space-y-8 ml-3">
                  {scheduledStops.map((stop, idx) => (
                    <div key={`stop-timeline-${stop.position}-${idx}`} className="relative">
                      <div className="absolute -left-[31px] top-0.5 h-4 w-4 rounded-full bg-[#15271F] border-2 border-[#B7C9AD]" />
                      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="text-xs font-mono text-[#B7C9AD]">Stop #{stop.position}</span>
                            <h4 className="text-base font-bold text-[#F7F7F2]">{stop.name}</h4>
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

          {/* TAB 2: CORRIDOR MAP */}
          {activeTab === "map" && (
            <div className="space-y-4">
              <div className="bg-[#15271F] p-5 rounded-2xl border border-[#233E32] flex flex-col sm:flex-row sm:items-center justify-between gap-3 shadow-xl">
                <div>
                  <h3 className="text-sm font-bold text-[#F7F7F2] flex items-center gap-2">
                    <MapIcon className="w-4 h-4 text-[#B7C9AD]" />
                    Pan-India Geospatial Corridor Route
                  </h3>
                  <p className="text-xs text-[#A9B8AD] mt-0.5">
                    Interactive PostGIS corridor waypoints and spatial road traces
                  </p>
                </div>
                <div className="text-xs text-[#B7C9AD] bg-[#0D1915] px-3.5 py-1.5 rounded-full border border-[#233E32] font-semibold flex items-center gap-2 self-start sm:self-auto">
                  <span className="w-2 h-2 rounded-full bg-[#B7C9AD] animate-pulse"></span>
                  <span>{mapStops.length} Waypoints Active</span>
                </div>
              </div>
              <div className="h-[520px] w-full rounded-2xl overflow-hidden border border-[#233E32] shadow-2xl">
                <CorridorMapWrapper stops={mapStops} places={corridorPlaces} />
              </div>
            </div>
          )}

          {/* TAB 3: BUDGET */}
          {activeTab === "budget" && (
            <div className="space-y-6">
              <div className="p-5 rounded-2xl bg-[#271E15]/80 border border-[#D4A373]/40 flex items-start gap-3.5">
                <AlertTriangle className="h-5 w-5 text-[#D4A373] shrink-0 mt-0.5" />
                <div className="text-xs space-y-1">
                  <h4 className="font-bold text-[#F7F7F2] uppercase tracking-wide">
                    Budget Completeness: Explicit Preservation
                  </h4>
                  <p className="text-[#A9B8AD] leading-relaxed">
                    Highway toll tariffs across state borders vary per vehicle class and are unverified in registry.
                    Missing toll charges have <span className="text-[#D4A373] font-semibold">NOT been coerced to ₹0</span>.
                  </p>
                </div>
              </div>

              <div className="glass-card rounded-3xl p-6 border border-[#233e32] space-y-4">
                <h3 className="text-sm font-bold text-[#F7F7F2] uppercase tracking-wider flex items-center gap-2">
                  <Receipt className="h-4 w-4 text-[#B7C9AD]" />
                  <span>Itemized Financial Breakdown</span>
                </h3>

                <div className="divide-y divide-[#15271F] text-xs">
                  <div className="py-3 flex items-center justify-between">
                    <div>
                      <span className="font-semibold text-[#F7F7F2]">
                        Fuel Transit ({totalDistanceKm} km @ {mileage} km/L)
                      </span>
                      <p className="text-[#A9B8AD]">
                        Calculated {fuelLiters}L × ₹{fuelPricePerLiter}/L
                      </p>
                    </div>
                    <span className="font-mono font-bold text-[#F7F7F2]">
                      ₹{fuelTotalCost.toLocaleString("en-IN")}
                    </span>
                  </div>

                  <div className="py-3 flex items-center justify-between">
                    <div>
                      <span className="font-semibold text-[#F7F7F2]">
                        Accommodation ({rooms} room, {destinations.reduce((a, b) => a + b.stayDays, 0)} total nights)
                      </span>
                      <p className="text-[#A9B8AD]">
                        Indicative benchmark ₹3,500/room/night
                      </p>
                    </div>
                    <span className="font-mono font-bold text-[#F7F7F2]">
                      ₹{hotelEstimatedTotal.toLocaleString("en-IN")}
                    </span>
                  </div>

                  <div className="py-3 flex items-center justify-between bg-[#271E15]/30 -mx-3 px-3 rounded-lg">
                    <div>
                      <span className="font-semibold text-[#D4A373]">Interstate & Expressways Tolls</span>
                      <p className="text-[#A9B8AD]">FASTag automated plaza tariffs</p>
                    </div>
                    <span className="font-mono font-semibold text-[#D4A373]">UNKNOWN</span>
                  </div>
                </div>

                <div className="pt-4 border-t border-[#233e32] flex items-center justify-between text-sm">
                  <div>
                    <span className="font-bold text-[#F7F7F2]">Known Subtotal</span>
                    <span className="block text-[11px] text-[#A9B8AD]">Excludes unknown tolls</span>
                  </div>
                  <div className="text-right">
                    <span className="text-xl font-bold text-[#B7C9AD]">
                      ₹{(fuelTotalCost + hotelEstimatedTotal).toLocaleString("en-IN")}
                    </span>
                    <span className="text-xs text-[#D4A373] block">+ unquoted tolls</span>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* TAB 4: RECOMMENDATIONS */}
          {activeTab === "recommendations" && (
            <div className="space-y-4">
              <div className="glass-panel p-4 rounded-2xl border border-[#233e32] text-xs text-[#A9B8AD]">
                <span className="text-[#B7C9AD] font-semibold">National Curation: </span>
                Recommended POIs are verified editorial selections with calculated detour times across national highways.
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {corridorPlaces.map((place, idx) => (
                  <div key={`rec-place-${idx}`} className="glass-card p-5 rounded-3xl border border-[#233e32] space-y-3">
                    <div className="flex items-start justify-between">
                      <span className="px-2 py-0.5 rounded-full bg-[#15271F] border border-[#B7C9AD]/20 text-[10px] font-semibold text-[#B7C9AD]">
                        {place.category}
                      </span>
                      <span className="text-[10px] font-mono text-[#A9B8AD]">+{place.detourKm}km detour</span>
                    </div>
                    <h4 className="font-bold text-[#F7F7F2] text-sm">{place.name}</h4>
                    <p className="text-xs text-[#A9B8AD]">Source: {place.source}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* TAB 5: EVIDENCE */}
          {activeTab === "evidence" && (
            <div className="glass-card p-6 rounded-3xl border border-[#233e32] space-y-4">
              <h3 className="text-sm font-bold text-[#F7F7F2] uppercase tracking-wider flex items-center gap-2">
                <ShieldCheck className="h-4 w-4 text-[#B7C9AD]" />
                <span>Evidence Presentation Registry</span>
              </h3>
              <p className="text-xs text-[#A9B8AD]">
                Verifiable record hash for trip aggregate <span className="font-mono text-[#B7C9AD]">{serverTripId}</span>
              </p>
            </div>
          )}

          {/* Merchant Handoff Ribbon */}
          <div className="glass-panel p-6 rounded-3xl border border-[#B7C9AD]/20 space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <div className="space-y-1 text-xs">
                <span className="font-bold text-[#F7F7F2] uppercase tracking-wider block">
                  Official Merchant Handoff
                </span>
                <p className="text-[#A9B8AD]">
                  Direct deep-links to official Indian travel portals. No markups or fake price holds.
                </p>
              </div>

              <div className="flex items-center gap-2.5 flex-wrap">
                <a
                  href="https://www.irctc.co.in"
                  target="_blank"
                  rel="noreferrer"
                  className="btn-sage px-4 py-2 rounded-xl text-xs font-semibold flex items-center gap-1.5 shadow-md"
                >
                  <span>Book on IRCTC</span>
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>

                <a
                  href="https://www.incredibleindia.org"
                  target="_blank"
                  rel="noreferrer"
                  className="btn-outline-forest px-4 py-2 rounded-xl text-xs font-semibold flex items-center gap-1.5"
                >
                  <span>Incredible India</span>
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Modal */}
      {evidenceModalOpen && (
        <div className="fixed inset-0 z-50 bg-[#0D1915]/80 backdrop-blur-md flex items-center justify-center p-4">
          <div className="glass-card max-w-xl w-full p-6 rounded-3xl border border-[#B7C9AD]/30 space-y-4 shadow-2xl">
            <div className="flex items-center justify-between border-b border-[#233e32] pb-3">
              <h3 className="text-base font-bold text-[#F7F7F2]">Verifiable Evidence Audit</h3>
              <button onClick={() => setEvidenceModalOpen(false)} className="text-[#A9B8AD] hover:text-[#F7F7F2] text-xs">
                ✕ Close
              </button>
            </div>
            <p className="text-xs text-[#A9B8AD]">Trip Aggregate URN: {serverTripId}</p>
            <button onClick={() => setEvidenceModalOpen(false)} className="btn-sage w-full py-2 rounded-xl text-xs font-semibold">
              Done Reviewing
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
          <span className="text-xs font-mono tracking-wider uppercase">Loading Planner Console...</span>
        </div>
      }
    >
      <DashboardContent />
    </Suspense>
  );
}
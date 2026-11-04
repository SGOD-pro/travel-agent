/**
 * Domain contracts for Trip Itinerary proposals conforming to spec 0007 Slice 6 (AC-8, AC-10, AC-11).
 */

import type { RouteMapGeoJsonResponse } from "./routing";

export interface ScheduleStop {
  position: number;
  name: string;
  latitude: number;
  longitude: number;
  arrival_time: string;
  departure_time: string;
  stay_duration_minutes: number;
  window_start?: string | null;
  window_end?: string | null;
  is_hours_assumed: boolean;
  assumed_daylight_hours?: boolean;
  hours_assumed?: boolean;
  hours_status: string;
}

export interface ScheduleLeg {
  from_stop: string;
  to_stop: string;
  duration_minutes: number;
  distance_km: number;
}

export interface ScheduleData {
  stops: ScheduleStop[];
  legs: ScheduleLeg[];
  total_travel_minutes: number;
  is_optimal: boolean;
}

export interface BudgetLine {
  category: string;
  amount: string | number | null;
  currency: string;
  basis: string;
  quantity?: string | number;
  source_ref?: string | null;
  estimated: boolean;
  unknown_reason?: string | null;
}

export interface BudgetSummary {
  known_total: {
    amount: string | number;
    currency: string;
  };
  is_complete: boolean;
  unknown_count: number;
  unknown_categories: string[];
}

export interface EditorialRecommendation {
  name?: string;
  primary_name?: string;
  category?: string;
  fit_score?: number;
  description?: string;
  uncertainty: string;
  source?: string | null;
  detour_km?: number;
  extra_duration_minutes?: number;
  latitude?: number;
  longitude?: number;
  lat?: number;
  lng?: number;
  rejection_reason?: string | null;
}

export interface WeatherAdvisoryStop {
  stop_name?: string;
  stopName?: string;
  latitude?: number;
  longitude?: number;
  temperature_celsius?: number;
  temperatureC?: number;
  condition: "sunny" | "cloudy" | "rainy" | "foggy" | string;
  humidity_percent?: number;
  humidityPercent?: number;
  monsoon_risk?: "low" | "moderate" | "severe" | string;
  monsoonRisk?: "low" | "moderate" | "severe" | string;
  ghat_caution?: string | null;
  ghatCaution?: string | null;
}

export interface ItineraryResponse {
  trip_id: string;
  version: number;
  state: string;
  status: string;
  schedule: ScheduleData | null;
  budget_summary: BudgetSummary | null;
  budget_lines: BudgetLine[];
  editorial_recommendations: EditorialRecommendation[];
  excluded_candidates: any[];
  weather_advisories: WeatherAdvisoryStop[];
  weather_status: string;
  validated_candidates: any[];
  discovered_places: any[];
  route_id: string | null;
  route_status: string | null;
  routing_status: string | null;
  route_diagnostics: string[];
  provider_diagnostics: Record<string, any>;
  map_geojson: RouteMapGeoJsonResponse | null;
  timezone?: string;
  generated_at: string | null;
}

export interface TripBriefDestination {
  name: string;
  latitude: number;
  longitude: number;
  stay_days?: number;
  visit_date?: string | null;
}

export interface TripBrief {
  origin_name: string;
  origin_lat: number;
  origin_lng: number;
  destinations: TripBriefDestination[];
  start_date: string;
  end_date: string;
  timezone?: string;
  adults?: number;
  rooms?: number;
  preferred_modes?: string[];
}

export interface TripResponse {
  id: string;
  owner_id: string;
  current_version: number;
  state: string;
  status?: string | null;
  brief?: TripBrief | null;
  current_route_id?: string | null;
}


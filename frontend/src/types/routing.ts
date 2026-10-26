/**
 * Domain types for Route Optimization, GeoJSON Map presentation, and Version Lineage.
 * Conforms to spec 0006 (AC-5, AC-7, AC-8, AC-10).
 */

export type RouteStatus =
  | "OPTIMAL"
  | "FEASIBLE"
  | "PARTIAL"
  | "INFEASIBLE"
  | "UNAVAILABLE";

export type TransportMode = "DRIVE" | "WALK" | "BICYCLE" | "TRANSIT";

export type StopType =
  | "ORIGIN"
  | "DESTINATION"
  | "WAYPOINT"
  | "PARKING_HUB"
  | "RETURN_ORIGIN";

export interface GeoJsonGeometry {
  type: "Point" | "LineString" | "Polygon" | "MultiPolygon";
  coordinates: any;
}

export interface StopFeatureProperties {
  feature_type: "stop";
  id: string;
  stop_order: number;
  name: string;
  title?: string;
  stop_name?: string;
  stop_type: string;
  planned_arrival_at: string | null;
  planned_departure_at: string | null;
  dwell_seconds: number;
  wait_seconds: number;
  is_completed?: boolean;
  is_skipped: boolean;
  skip_reason: string | null;
  "marker-color": string;
  "marker-symbol": string;
}

export interface LegFeatureProperties {
  feature_type: "leg";
  leg_order: number;
  from_stop_id: string;
  to_stop_id: string;
  transport_mode: string;
  mode?: string;
  distance_m: number;
  travel_seconds: number;
  traffic_basis?: string;
  stroke: string;
  "stroke-width": number;
  "stroke-dasharray": string;
}

export interface RestrictionFeatureProperties {
  feature_type: "restriction";
  restriction_type: string;
  fill: string;
  "fill-opacity": number;
  stroke: string;
  description: string;
}

export interface GeoJsonFeature {
  type: "Feature";
  geometry: GeoJsonGeometry;
  properties:
    | StopFeatureProperties
    | LegFeatureProperties
    | RestrictionFeatureProperties
    | Record<string, any>;
}

export interface RouteMapGeoJsonResponse {
  type: "FeatureCollection";
  bbox: [number, number, number, number] | null;
  features: GeoJsonFeature[];
  properties: {
    route_id: string;
    route_version: number;
    parent_route_id: string | null;
    status: RouteStatus;
    solver_status: string;
    total_distance_m: number;
    total_travel_seconds: number;
    fuel_cost: string | null;
    warnings: string[];
    diagnostics: string[];
  };
  route_id?: string;
  route_version?: number;
  parent_route_id?: string | null;
  status?: RouteStatus;
  solver_status?: string;
  total_distance_m?: number;
  total_travel_seconds?: number;
  fuel_cost?: string | null;
  warnings?: string[];
  diagnostics?: string[];
}

export interface RouteVersionItem {
  route_id: string;
  route_version: number;
  parent_route_id: string | null;
  created_at: string;
  status: RouteStatus;
  total_distance_m: number;
  total_travel_seconds: number;
  distance_delta_m: number | null;
  duration_delta_seconds: number | null;
  dropped_stop_ids: string[];
  replan_origin: [number, number] | null;
}

export interface RouteVersionHistoryResponse {
  root_route_id: string;
  current_route_id: string;
  versions: RouteVersionItem[];
}

export interface OptimizationJobResponse {
  job_id: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
  workflow: string;
  created_at: string;
  updated_at: string;
  result_available: boolean;
  result_ref: {
    route_id?: string;
    map_url?: string;
    [key: string]: any;
  } | null;
  error: {
    message?: string;
    code?: string;
    [key: string]: any;
  } | null;
}

"use client";

import React, { useEffect, useRef } from "react";
import L from "leaflet";
import type {
  RouteMapGeoJsonResponse,
  StopFeatureProperties,
  LegFeatureProperties,
  RestrictionFeatureProperties,
} from "@/types/routing";

export interface MapStop {
  name: string;
  lat: number;
  lng: number;
  stayMinutes?: number;
  arrivalTime?: string;
  departureTime?: string;
}

export interface MapPlace {
  name: string;
  category: string;
  fitScore: number;
  detourKm: number;
  extraDurationMins: number;
  source: string;
  uncertainty: string;
  lat: number;
  lng: number;
  description?: string;
}

export interface CorridorMapProps {
  geoJson?: RouteMapGeoJsonResponse | null;
  stops?: MapStop[];
  places?: MapPlace[];
  activeStopIndex?: number;
  onSelectStop?: (stopId: string) => void;
  onSelectPlace?: (place: MapPlace) => void;
}

/**
 * Constructs a DOM Element for stop popups with textContent escaping to prevent XSS.
 */
function createSafeStopPopup(props: StopFeatureProperties): HTMLElement {
  const container = document.createElement("div");
  container.className = "swena-safe-stop-popup";
  container.style.cssText =
    "padding: 6px 8px; color: #F7F7F2; font-family: inherit; min-width: 180px;";

  const header = document.createElement("div");
  header.style.cssText =
    "display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; gap: 8px;";

  const orderSpan = document.createElement("span");
  orderSpan.style.cssText =
    "font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; color: #A9B8AD; font-weight: 600;";
  orderSpan.textContent = `Stop ${props.stop_order}`;
  header.appendChild(orderSpan);

  const typeBadge = document.createElement("span");
  typeBadge.style.cssText =
    "font-size: 9px; padding: 2px 6px; border-radius: 4px; font-weight: 700; letter-spacing: 0.03em;";
  if (props.is_skipped) {
    typeBadge.style.backgroundColor = "rgba(220, 38, 38, 0.25)";
    typeBadge.style.color = "#f87171";
    typeBadge.textContent = "SKIPPED";
  } else if (props.is_completed) {
    typeBadge.style.backgroundColor = "rgba(5, 150, 105, 0.25)";
    typeBadge.style.color = "#34d399";
    typeBadge.textContent = "COMPLETED";
  } else if (props.stop_type === "PARKING_HUB") {
    typeBadge.style.backgroundColor = "rgba(217, 119, 6, 0.25)";
    typeBadge.style.color = "#fbbf24";
    typeBadge.textContent = "PARKING";
  } else {
    typeBadge.style.backgroundColor = "rgba(183, 201, 173, 0.2)";
    typeBadge.style.color = "#B7C9AD";
    typeBadge.textContent = props.stop_type;
  }
  header.appendChild(typeBadge);
  container.appendChild(header);

  const title = document.createElement("div");
  title.style.cssText =
    "font-weight: 700; font-size: 13px; color: #F7F7F2; margin-bottom: 4px; line-height: 1.3;";
  title.textContent = props.name || props.title || props.stop_name || "Destination";
  container.appendChild(title);

  if (props.planned_arrival_at) {
    const arr = document.createElement("div");
    arr.style.cssText = "font-size: 11px; color: #B7C9AD; margin-top: 2px;";
    arr.textContent = `Arrival: ${new Date(props.planned_arrival_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    container.appendChild(arr);
  }

  if (props.planned_departure_at) {
    const dep = document.createElement("div");
    dep.style.cssText = "font-size: 11px; color: #A9B8AD; margin-top: 1px;";
    dep.textContent = `Departure: ${new Date(props.planned_departure_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    container.appendChild(dep);
  }

  if (props.dwell_seconds > 0) {
    const dwell = document.createElement("div");
    dwell.style.cssText = "font-size: 11px; color: #8F9E93; margin-top: 2px;";
    dwell.textContent = `Stay: ${Math.round(props.dwell_seconds / 60)} mins`;
    container.appendChild(dwell);
  }

  if (props.is_skipped && props.skip_reason) {
    const reasonBox = document.createElement("div");
    reasonBox.style.cssText =
      "margin-top: 6px; padding: 4px 6px; border-radius: 4px; background: rgba(220, 38, 38, 0.15); border: 1px solid rgba(220, 38, 38, 0.3); font-size: 10px; color: #fca5a5;";
    reasonBox.textContent = `Skip Reason: ${props.skip_reason}`;
    container.appendChild(reasonBox);
  }

  return container;
}

/**
 * Constructs a DOM Element for leg polylines with textContent escaping.
 */
function createSafeLegPopup(props: LegFeatureProperties): HTMLElement {
  const container = document.createElement("div");
  container.className = "swena-safe-leg-popup";
  container.style.cssText = "padding: 4px 6px; color: #F7F7F2; font-family: inherit;";

  const mode = document.createElement("div");
  mode.style.cssText = "font-weight: 700; font-size: 12px; margin-bottom: 2px;";
  const isWalking = props.transport_mode.toUpperCase().includes("WALK");
  mode.style.color = isWalking ? "#10b981" : "#3b82f6";
  mode.textContent = `Leg ${props.leg_order}: ${props.transport_mode}`;
  container.appendChild(mode);

  const stats = document.createElement("div");
  stats.style.cssText = "font-size: 11px; color: #A9B8AD;";
  const km = (props.distance_m / 1000).toFixed(1);
  const mins = Math.round(props.travel_seconds / 60);
  stats.textContent = `${km} km • ~${mins} mins`;
  container.appendChild(stats);

  if (props.traffic_basis) {
    const traffic = document.createElement("div");
    traffic.style.cssText = "font-size: 10px; color: #8F9E93; margin-top: 2px;";
    traffic.textContent = `Traffic: ${props.traffic_basis}`;
    container.appendChild(traffic);
  }

  return container;
}

/**
 * Constructs a DOM Element for road restriction polygons with textContent escaping.
 */
function createSafeRestrictionPopup(props: RestrictionFeatureProperties): HTMLElement {
  const container = document.createElement("div");
  container.className = "swena-safe-restriction-popup";
  container.style.cssText = "padding: 4px 6px; color: #F7F7F2; font-family: inherit;";

  const badge = document.createElement("div");
  badge.style.cssText = "font-weight: 700; font-size: 12px; color: #ef4444; margin-bottom: 2px;";
  badge.textContent = `Restriction: ${props.restriction_type}`;
  container.appendChild(badge);

  const desc = document.createElement("div");
  desc.style.cssText = "font-size: 11px; color: #fca5a5; line-height: 1.3;";
  desc.textContent = props.description || "Active road closure avoidance area";
  container.appendChild(desc);

  return container;
}

export default function CorridorMap({
  geoJson,
  stops = [],
  places = [],
  activeStopIndex = 0,
  onSelectStop,
  onSelectPlace,
}: CorridorMapProps) {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapInstanceRef = useRef<L.Map | null>(null);
  const layerGroupRef = useRef<L.LayerGroup | null>(null);

  useEffect(() => {
    if (!mapContainerRef.current || mapInstanceRef.current) return;

    // Center on Karnataka corridor by default
    const initialLat = stops[0]?.lat ?? 12.9716;
    const initialLng = stops[0]?.lng ?? 77.5946;

    const map = L.map(mapContainerRef.current, {
      center: [initialLat, initialLng],
      zoom: 8,
      zoomControl: true,
      attributionControl: true,
    });

    // Sleek CartoDB Dark Matter tiles with strictly preserved OpenStreetMap and CARTO attribution
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      maxZoom: 19,
      subdomains: "abcd",
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions" target="_blank" rel="noopener noreferrer">CARTO</a>',
    }).addTo(map);

    const layerGroup = L.layerGroup().addTo(map);
    mapInstanceRef.current = map;
    layerGroupRef.current = layerGroup;

    return () => {
      map.remove();
      mapInstanceRef.current = null;
      layerGroupRef.current = null;
    };
  }, []);

  // Render authoritative GeoJSON FeatureCollection when available, else fallback to legacy stops/places
  useEffect(() => {
    const map = mapInstanceRef.current;
    const layerGroup = layerGroupRef.current;
    if (!map || !layerGroup) return;

    layerGroup.clearLayers();
    const allCoords: [number, number][] = [];

    if (geoJson && Array.isArray(geoJson.features) && geoJson.features.length > 0) {
      // 1. Render Restriction Polygons first (underneath legs and pins)
      geoJson.features.forEach((feat) => {
        if (feat.properties.feature_type === "restriction") {
          const props = feat.properties as RestrictionFeatureProperties;
          const geom = feat.geometry;

          if (geom.type === "Polygon" && Array.isArray(geom.coordinates)) {
            const latLngRings = geom.coordinates.map((ring: number[][]) =>
              ring.map(([lon, lat]: number[]) => {
                allCoords.push([lat, lon]);
                return [lat, lon] as [number, number];
              })
            );
            const polygon = L.polygon(latLngRings, {
              color: props.stroke || "#b91c1c",
              weight: 2,
              fillColor: props.fill || "#ef4444",
              fillOpacity: props["fill-opacity"] ?? 0.35,
            });
            polygon.bindPopup(createSafeRestrictionPopup(props));
            polygon.addTo(layerGroup);
          } else if (geom.type === "MultiPolygon" && Array.isArray(geom.coordinates)) {
            const multiLatLngs = geom.coordinates.map((poly: number[][][]) =>
              poly.map((ring: number[][]) =>
                ring.map(([lon, lat]: number[]) => {
                  allCoords.push([lat, lon]);
                  return [lat, lon] as [number, number];
                })
              )
            );
            const polygon = L.polygon(multiLatLngs, {
              color: props.stroke || "#b91c1c",
              weight: 2,
              fillColor: props.fill || "#ef4444",
              fillOpacity: props["fill-opacity"] ?? 0.35,
            });
            polygon.bindPopup(createSafeRestrictionPopup(props));
            polygon.addTo(layerGroup);
          }
        }
      });

      // 2. Render Leg LineStrings
      geoJson.features.forEach((feat) => {
        if (feat.properties.feature_type === "leg") {
          const props = feat.properties as LegFeatureProperties;
          const geom = feat.geometry;

          if (geom.type === "LineString" && Array.isArray(geom.coordinates)) {
            const latLngs: [number, number][] = geom.coordinates.map(
              ([lon, lat]: number[]) => {
                allCoords.push([lat, lon]);
                return [lat, lon];
              }
            );

            if (latLngs.length >= 2) {
              const strokeColor = props.stroke || "#3b82f6";
              const strokeWidth = props["stroke-width"] || 4;
              const dashArray =
                props["stroke-dasharray"] && props["stroke-dasharray"] !== "none"
                  ? props["stroke-dasharray"]
                  : undefined;

              // Subtle glowing underlayer line
              L.polyline(latLngs, {
                color: strokeColor,
                weight: strokeWidth + 4,
                opacity: 0.25,
                lineCap: "round",
                lineJoin: "round",
              }).addTo(layerGroup);

              // Main active polyline
              const line = L.polyline(latLngs, {
                color: strokeColor,
                weight: strokeWidth,
                opacity: 0.95,
                dashArray: dashArray,
                lineCap: "round",
                lineJoin: "round",
              });
              line.bindPopup(createSafeLegPopup(props));
              line.addTo(layerGroup);
            }
          }
        }
      });

      // 3. Render Stop Points with custom color markers and sequence symbols
      geoJson.features.forEach((feat) => {
        if (feat.properties.feature_type === "stop") {
          const props = feat.properties as StopFeatureProperties;
          const geom = feat.geometry;

          if (geom.type === "Point" && Array.isArray(geom.coordinates)) {
            const [lon, lat] = geom.coordinates;
            allCoords.push([lat, lon]);

            const markerColor = props["marker-color"] || "#2563eb";
            const rawSymbol = props["marker-symbol"] || String(props.stop_order);
            const markerSymbol = String(rawSymbol).replace(/[^\w\s-]/g, "").slice(0, 8);
            const isSkippedOrInfeasible = props.is_skipped;

            const stopIcon = L.divIcon({
              className: "custom-geojson-stop-marker",
              html: `
                <div style="
                  width: 32px;
                  height: 32px;
                  border-radius: 50%;
                  background-color: ${markerColor};
                  color: #ffffff;
                  border: 2px solid ${isSkippedOrInfeasible ? "#fca5a5" : "#F7F7F2"};
                  display: flex;
                  align-items: center;
                  justify-content: center;
                  font-weight: 800;
                  font-size: ${markerSymbol === "P" ? "14px" : "12px"};
                  box-shadow: 0 0 16px ${markerColor}66, 0 2px 6px rgba(0,0,0,0.6);
                  cursor: pointer;
                  transition: transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
                ">
                  ${markerSymbol}
                </div>
              `,
              iconSize: [32, 32],
              iconAnchor: [16, 16],
              popupAnchor: [0, -18],
            });

            const marker = L.marker([lat, lon], { icon: stopIcon })
              .bindPopup(createSafeStopPopup(props))
              .addTo(layerGroup);

            if (onSelectStop) {
              marker.on("click", () => onSelectStop(props.id));
            }
          }
        }
      });

      // Fit bounds using GeoJSON bbox or accumulated coordinates
      if (geoJson.bbox && geoJson.bbox.length === 4) {
        const [minLon, minLat, maxLon, maxLat] = geoJson.bbox;
        map.fitBounds(
          [
            [minLat, minLon],
            [maxLat, maxLon],
          ],
          { padding: [40, 40], maxZoom: 14 }
        );
      } else if (allCoords.length > 0) {
        map.fitBounds(L.latLngBounds(allCoords), { padding: [40, 40], maxZoom: 14 });
      }
    } else {
      // Fallback: Legacy stops and places rendering
      if (stops.length > 1) {
        const stopPoints: [number, number][] = stops.map((s) => [s.lat, s.lng]);
        L.polyline(stopPoints, {
          color: "rgba(183, 201, 173, 0.25)",
          weight: 8,
          lineCap: "round",
          lineJoin: "round",
        }).addTo(layerGroup);

        L.polyline(stopPoints, {
          color: "#B7C9AD",
          weight: 3.5,
          opacity: 0.95,
          dashArray: "6, 8",
          lineCap: "round",
          lineJoin: "round",
        }).addTo(layerGroup);
      }

      stops.forEach((stop, index) => {
        allCoords.push([stop.lat, stop.lng]);
        const isActive = index === activeStopIndex;

        const stopIcon = L.divIcon({
          className: "custom-stop-marker",
          html: `
            <div style="
              width: 32px;
              height: 32px;
              border-radius: 50%;
              background-color: ${isActive ? "#B7C9AD" : "#15271F"};
              color: ${isActive ? "#102D25" : "#F7F7F2"};
              border: 2px solid #B7C9AD;
              display: flex;
              align-items: center;
              justify-content: center;
              font-weight: 700;
              font-size: 13px;
              box-shadow: 0 0 16px rgba(183, 201, 173, 0.4);
              cursor: pointer;
            ">
              ${index + 1}
            </div>
          `,
          iconSize: [32, 32],
          iconAnchor: [16, 16],
          popupAnchor: [0, -16],
        });

        const stopProps: StopFeatureProperties = {
          feature_type: "stop",
          id: `stop-${index}`,
          stop_order: index + 1,
          name: stop.name,
          stop_type: "VENUE",
          planned_arrival_at: null,
          planned_departure_at: null,
          dwell_seconds: (stop.stayMinutes || 0) * 60,
          wait_seconds: 0,
          is_skipped: false,
          skip_reason: null,
          "marker-color": "#B7C9AD",
          "marker-symbol": String(index + 1),
        };

        L.marker([stop.lat, stop.lng], { icon: stopIcon })
          .bindPopup(createSafeStopPopup(stopProps))
          .addTo(layerGroup);
      });

      places.forEach((place) => {
        allCoords.push([place.lat, place.lng]);

        const poiIcon = L.divIcon({
          className: "custom-poi-marker",
          html: `
            <div style="
              width: 22px;
              height: 22px;
              border-radius: 6px;
              background-color: #102D25;
              color: #B7C9AD;
              border: 1.5px solid #B7C9AD;
              display: flex;
              align-items: center;
              justify-content: center;
              font-size: 11px;
              box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
              cursor: pointer;
              transform: rotate(45deg);
            ">
              <div style="transform: rotate(-45deg); font-weight: bold;">✦</div>
            </div>
          `,
          iconSize: [22, 22],
          iconAnchor: [11, 11],
          popupAnchor: [0, -12],
        });

        const popupDiv = document.createElement("div");
        popupDiv.style.cssText = "padding: 4px 6px; color: #F7F7F2;";
        const nameDiv = document.createElement("div");
        nameDiv.style.fontWeight = "700";
        nameDiv.textContent = place.name;
        popupDiv.appendChild(nameDiv);

        const marker = L.marker([place.lat, place.lng], { icon: poiIcon })
          .bindPopup(popupDiv)
          .addTo(layerGroup);

        if (onSelectPlace) {
          marker.on("click", () => onSelectPlace(place));
        }
      });

      if (allCoords.length > 0) {
        map.fitBounds(L.latLngBounds(allCoords), { padding: [40, 40], maxZoom: 11 });
      }
    }
  }, [geoJson, stops, places, activeStopIndex, onSelectStop, onSelectPlace]);

  // Derive route summary metrics from GeoJSON
  const routeStatus = geoJson?.properties?.status || geoJson?.status;
  const totalDistM =
    geoJson?.properties?.total_distance_m ?? geoJson?.total_distance_m;
  const totalTravelS =
    geoJson?.properties?.total_travel_seconds ?? geoJson?.total_travel_seconds;
  const fuelCost = geoJson?.properties?.fuel_cost ?? geoJson?.fuel_cost;
  const diagnostics =
    geoJson?.properties?.diagnostics || geoJson?.diagnostics || [];

  const statusBadgeStyle =
    routeStatus === "FEASIBLE" || routeStatus === "OPTIMAL"
      ? "bg-emerald-950/80 text-emerald-300 border-emerald-700/60"
      : routeStatus === "PARTIAL"
        ? "bg-amber-950/80 text-amber-300 border-amber-700/60"
        : routeStatus === "INFEASIBLE"
          ? "bg-red-950/80 text-red-300 border-red-700/60"
          : "bg-[#15271F]/80 text-[#A9B8AD] border-[#233E32]";

  return (
    <div className="relative w-full h-full min-h-105 rounded-2xl overflow-hidden border border-[#233E32] bg-[#0D1915]">
      {/* Map Canvas */}
      <div ref={mapContainerRef} className="w-full h-full min-h-105" />

      {/* Top Floating Overlay: Route Status Badge and Metrics */}
      {routeStatus && (
        <div className="absolute top-3 left-3 z-[1000] bg-[#15271F]/90 backdrop-blur-md px-3.5 py-2 rounded-xl border border-[rgba(183,201,173,0.2)] flex flex-wrap items-center gap-3 shadow-lg">
          <span
            className={`px-2 py-0.5 rounded-md text-[10px] font-bold uppercase tracking-wider border ${statusBadgeStyle}`}
          >
            {routeStatus}
          </span>

          {totalDistM !== undefined && (
            <div className="text-[11px] text-[#F7F7F2] font-semibold">
              {(totalDistM / 1000).toFixed(1)}{" "}
              <span className="text-[10px] text-[#A9B8AD] font-normal">km</span>
            </div>
          )}

          {totalTravelS !== undefined && (
            <div className="text-[11px] text-[#F7F7F2] font-semibold">
              {Math.floor(totalTravelS / 3600) > 0 &&
                `${Math.floor(totalTravelS / 3600)}h `}
              {Math.round((totalTravelS % 3600) / 60)}m
            </div>
          )}

          {fuelCost && (
            <div className="text-[11px] text-[#B7C9AD] font-semibold">
              ₹{fuelCost}
            </div>
          )}
        </div>
      )}

      {/* Top Right Floating Legend */}
      <div className="absolute top-3 right-3 z-[1000] bg-[#15271F]/90 backdrop-blur-md px-3 py-1.5 rounded-lg border border-[rgba(183,201,173,0.2)] text-[10px] text-[#A9B8AD] flex items-center gap-3 shadow-lg">
        <div className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-[#3b82f6] inline-block"></span>
          <span>Drive</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-[#10b981] inline-block"></span>
          <span>Walk</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-[#d97706] inline-block"></span>
          <span>Parking</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-[#dc2626] inline-block"></span>
          <span>Skipped</span>
        </div>
      </div>

      {/* Bottom Diagnostics Banner if Infeasible or Warnings */}
      {diagnostics.length > 0 && routeStatus === "INFEASIBLE" && (
        <div className="absolute bottom-3 left-3 right-3 z-[1000] bg-red-950/90 backdrop-blur-md px-4 py-2.5 rounded-xl border border-red-700/60 text-xs text-red-200 shadow-xl flex items-start gap-2.5">
          <span className="font-bold uppercase tracking-wider text-red-400 text-[10px] mt-0.5">
            Notice:
          </span>
          <div className="flex-1 space-y-1">
            {diagnostics.map((diag, idx) => (
              <div key={idx} className="leading-snug">
                {diag}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

"use client";

import React, { useEffect, useRef } from "react";
import L from "leaflet";

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

interface CorridorMapProps {
  stops: MapStop[];
  places?: MapPlace[];
  activeStopIndex?: number;
  onSelectPlace?: (place: MapPlace) => void;
}

export default function CorridorMap({
  stops,
  places = [],
  activeStopIndex = 0,
  onSelectPlace,
}: CorridorMapProps) {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapInstanceRef = useRef<L.Map | null>(null);
  const layerGroupRef = useRef<L.LayerGroup | null>(null);

  useEffect(() => {
    if (!mapContainerRef.current || mapInstanceRef.current) return;

    // Default center to Karnataka corridor
    const initialLat = stops[0]?.lat ?? 12.9716;
    const initialLng = stops[0]?.lng ?? 77.5946;

    const map = L.map(mapContainerRef.current, {
      center: [initialLat, initialLng],
      zoom: 8,
      zoomControl: true,
      attributionControl: false,
    });

    // Sleek CartoDB Dark Matter tiles matching SWENA forest-night
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      maxZoom: 19,
      subdomains: "abcd",
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

  // Update markers, corridors, and bounds when stops or places change
  useEffect(() => {
    const map = mapInstanceRef.current;
    const layerGroup = layerGroupRef.current;
    if (!map || !layerGroup) return;

    layerGroup.clearLayers();
    const latLngs: L.LatLngExpression[] = [];

    // 1. Draw corridor route polyline connecting stops
    if (stops.length > 1) {
      const stopPoints: [number, number][] = stops.map((s) => [s.lat, s.lng]);
      
      // Outer subtle glow line
      L.polyline(stopPoints, {
        color: "rgba(183, 201, 173, 0.25)",
        weight: 8,
        lineCap: "round",
        lineJoin: "round",
      }).addTo(layerGroup);

      // Inner sage corridor line
      L.polyline(stopPoints, {
        color: "#B7C9AD",
        weight: 3.5,
        opacity: 0.95,
        dashArray: "6, 8",
        lineCap: "round",
        lineJoin: "round",
      }).addTo(layerGroup);
    }

    // 2. Add Stop Markers
    stops.forEach((stop, index) => {
      latLngs.push([stop.lat, stop.lng]);
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
            transition: transform 0.2s ease;
          ">
            ${index + 1}
          </div>
        `,
        iconSize: [32, 32],
        iconAnchor: [16, 16],
        popupAnchor: [0, -16],
      });

      const popupContent = `
        <div style="padding: 4px 6px;">
          <div style="font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; color: #A9B8AD; margin-bottom: 2px;">
            Stop ${index + 1}
          </div>
          <div style="font-weight: 700; font-size: 14px; color: #F7F7F2; margin-bottom: 4px;">
            ${stop.name}
          </div>
          ${
            stop.arrivalTime
              ? `<div style="font-size: 12px; color: #B7C9AD;">Arrival: ${stop.arrivalTime}</div>`
              : ""
          }
          ${
            stop.departureTime
              ? `<div style="font-size: 12px; color: #A9B8AD;">Departure: ${stop.departureTime}</div>`
              : ""
          }
          ${
            stop.stayMinutes
              ? `<div style="font-size: 11px; color: #8F9E93; margin-top: 4px;">Stay duration: ${Math.round(
                  stop.stayMinutes / 60
                )} hrs</div>`
              : ""
          }
        </div>
      `;

      L.marker([stop.lat, stop.lng], { icon: stopIcon })
        .bindPopup(popupContent)
        .addTo(layerGroup);
    });

    // 3. Add Corridor Places / POIs
    places.forEach((place) => {
      latLngs.push([place.lat, place.lng]);

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

      const popupContent = `
        <div style="padding: 4px 6px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
            <span style="font-size: 10px; color: #B7C9AD; background: rgba(183, 201, 173, 0.15); padding: 2px 6px; border-radius: 4px;">
              Fit: ${(place.fitScore * 100).toFixed(0)}%
            </span>
            <span style="font-size: 10px; color: #A9B8AD;">
              +${place.detourKm} km (${place.extraDurationMins}m)
            </span>
          </div>
          <div style="font-weight: 700; font-size: 13px; color: #F7F7F2; margin-bottom: 2px;">
            ${place.name}
          </div>
          <div style="font-size: 11px; color: #A9B8AD; margin-bottom: 4px;">
            ${place.category} • ${place.source}
          </div>
          <div style="font-size: 10px; color: #D1B06E;">
            Tier: ${place.uncertainty}
          </div>
        </div>
      `;

      const marker = L.marker([place.lat, place.lng], { icon: poiIcon })
        .bindPopup(popupContent)
        .addTo(layerGroup);

      if (onSelectPlace) {
        marker.on("click", () => onSelectPlace(place));
      }
    });

    // Fit map bounds
    if (latLngs.length > 0) {
      map.fitBounds(L.latLngBounds(latLngs), { padding: [40, 40], maxZoom: 11 });
    }
  }, [stops, places, activeStopIndex, onSelectPlace]);

  return (
    <div className="relative w-full h-full min-h-[380px] rounded-2xl overflow-hidden border border-[#233E32] bg-[#0D1915]">
      <div ref={mapContainerRef} className="w-full h-full min-h-[380px]" />
      <div className="absolute top-3 right-3 z-[1000] bg-[#15271F]/90 backdrop-blur-md px-3 py-1.5 rounded-lg border border-[rgba(183,201,173,0.2)] text-[11px] text-[#A9B8AD] flex items-center gap-3">
        <div className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-[#B7C9AD] inline-block"></span>
          <span>Corridor Stop</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rotate-45 bg-[#102D25] border border-[#B7C9AD] inline-block"></span>
          <span>Corridor POI</span>
        </div>
      </div>
    </div>
  );
}

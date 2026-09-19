"use client";

import React from "react";
import { CloudRain, Sun, Cloud, AlertTriangle, ShieldCheck, Wind } from "lucide-react";

export interface StopWeather {
  stopName: string;
  temperatureC: number;
  condition: "sunny" | "cloudy" | "rainy" | "foggy";
  humidityPercent: number;
  monsoonRisk: "low" | "moderate" | "severe";
  ghatCaution?: string;
}

interface CorridorWeatherAdvisoryProps {
  stops: StopWeather[];
}

export default function CorridorWeatherAdvisory({ stops }: CorridorWeatherAdvisoryProps) {
  const getConditionIcon = (condition: StopWeather["condition"]) => {
    switch (condition) {
      case "sunny":
        return <Sun className="w-4 h-4 text-amber-400" />;
      case "rainy":
        return <CloudRain className="w-4 h-4 text-blue-400" />;
      case "foggy":
        return <Wind className="w-4 h-4 text-[#A9B8AD]" />;
      case "cloudy":
      default:
        return <Cloud className="w-4 h-4 text-[#B7C9AD]" />;
    }
  };

  const getRiskBadge = (risk: StopWeather["monsoonRisk"]) => {
    switch (risk) {
      case "severe":
        return (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold bg-red-500/20 text-red-400 border border-red-500/30">
            <AlertTriangle className="w-2.5 h-2.5" /> Ghat Alert
          </span>
        );
      case "moderate":
        return (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-500/20 text-amber-400 border border-amber-500/30">
            <AlertTriangle className="w-2.5 h-2.5" /> Rain Caution
          </span>
        );
      case "low":
      default:
        return (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
            <ShieldCheck className="w-2.5 h-2.5" /> Optimal
          </span>
        );
    }
  };

  return (
    <div className="bg-[#15271F] rounded-2xl border border-[#233E32] p-5 shadow-xl">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4 border-b border-[#233E32]/60 pb-3">
        <div>
          <h3 className="text-sm font-semibold text-[#F7F7F2] flex items-center gap-2">
            <CloudRain className="w-4 h-4 text-[#B7C9AD]" />
            Corridor Weather & Ghat Advisory
          </h3>
          <p className="text-xs text-[#A9B8AD] mt-0.5">
            Regional terrain conditions & monsoon safety index along the route
          </p>
        </div>
        <div className="text-[11px] text-[#A9B8AD] bg-[#0D1915] px-3 py-1 rounded-full border border-[#233E32]">
          Updated live for travel window
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3.5">
        {stops.map((stop, idx) => (
          <div
            key={idx}
            className="bg-[#0D1915] p-3.5 rounded-xl border border-[#233E32] flex flex-col justify-between"
          >
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="font-medium text-sm text-[#F7F7F2] truncate">{stop.stopName}</span>
                {getRiskBadge(stop.monsoonRisk)}
              </div>

              <div className="flex items-center gap-3 my-2">
                <div className="p-2 rounded-lg bg-[#15271F] border border-[#233E32]">
                  {getConditionIcon(stop.condition)}
                </div>
                <div>
                  <div className="text-lg font-bold text-[#F7F7F2]">{stop.temperatureC}°C</div>
                  <div className="text-xs text-[#A9B8AD] capitalize">{stop.condition} • {stop.humidityPercent}% Hum</div>
                </div>
              </div>
            </div>

            {stop.ghatCaution && (
              <div className="mt-2 text-[11px] text-[#A9B8AD] bg-[#15271F]/80 p-2 rounded-lg border border-[#233E32] flex items-start gap-1.5">
                <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5" />
                <span>{stop.ghatCaution}</span>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

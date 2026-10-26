"use client";

import dynamic from "next/dynamic";
import type { CorridorMapProps } from "./CorridorMap";

const CorridorMap = dynamic(() => import("./CorridorMap"), {
  ssr: false,
  loading: () => (
    <div className="w-full h-full min-h-105 rounded-2xl bg-[#15271F] flex flex-col items-center justify-center border border-[#233E32] text-[#A9B8AD] gap-3">
      <div className="w-8 h-8 rounded-full border-2 border-[#B7C9AD] border-t-transparent animate-spin" />
      <span className="text-xs uppercase tracking-wider">
        Loading Corridor Geospatial Canvas...
      </span>
    </div>
  ),
});

export type CorridorMapWrapperProps = CorridorMapProps;

export default function CorridorMapWrapper(props: CorridorMapWrapperProps) {
  return <CorridorMap {...props} />;
}

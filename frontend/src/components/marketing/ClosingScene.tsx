"use client";

import React from "react";
import Image from "next/image";
import Link from "next/link";
import { ArrowRight, MapPin } from "lucide-react";

export function ClosingScene() {
  return (
    <section
      className="relative min-h-[75svh] flex flex-col justify-center items-center px-6 sm:px-12 text-center overflow-hidden border-b border-[#233E32]"
      aria-label="Scene 6: Closing Invitation"
    >
      {/* Background Photography: Konkan Sunset */}
      <div className="absolute inset-0 z-0">
        <Image
          src="/images/destinations/konkan-sunset.jpg"
          alt="Golden coastal sunset over rocks and Arabian Sea, Kudle Beach, Gokarna"
          fill
          sizes="100vw"
          className="object-cover object-center"
        />
        {/* Scrim Overlay */}
        <div className="absolute inset-0 bg-gradient-to-t from-[#0A1411] via-[#0D1915]/75 to-[#0D1915]/60" />
      </div>

      {/* Narrative Invitation */}
      <div className="relative z-10 max-w-3xl mx-auto space-y-6 py-16">
        <div className="inline-flex items-center gap-2 px-3.5 py-1 rounded-full bg-[#15271F]/80 backdrop-blur-md border border-[#B7C9AD]/25 text-xs font-mono text-[#B7C9AD] uppercase tracking-wider">
          <MapPin className="h-3.5 w-3.5 text-[#B7C9AD]" />
          <span>Gokarna, Konkan Coast • 14.5298° N, 74.3160° E</span>
        </div>

        <h2 className="font-serif text-4xl sm:text-6xl lg:text-7xl font-bold tracking-tight text-[#F7F7F2] leading-[1.02]">
          Where will you go next?
        </h2>

        <p className="text-base sm:text-xl text-[#A9B8AD] max-w-lg mx-auto font-normal leading-relaxed">
          Less planning. More remembering. Bring your driving route, daily stops, and transparent budget assumptions into one calm workspace.
        </p>

        <div className="pt-4">
          <Link
            href="/dashboard"
            className="inline-flex items-center gap-2 rounded-xl bg-[#B7C9AD] px-8 py-4 text-sm font-semibold text-[#102D25] hover:bg-[#C9DBBE] transition-all shadow-2xl shadow-black/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD] cursor-pointer"
          >
            <span>Plan my trip</span>
            <ArrowRight className="h-4 w-4" />
          </Link>
        </div>

        <p className="text-xs text-[#6E8274] pt-4 font-mono">
          Non-custodial intelligence • Direct official supplier handoff
        </p>
      </div>
    </section>
  );
}

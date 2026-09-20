"use client";

import React from "react";
import Image from "next/image";
import { Compass, Sparkles } from "lucide-react";

export function EditorialPause() {
  return (
    <section
      className="relative py-24 sm:py-36 px-6 sm:px-12 bg-[#F3EFE6] text-[#142820] border-b border-[#E8E1D4] overflow-hidden"
      aria-label="Scene 3: Editorial Pause"
    >
      <div className="mx-auto max-w-6xl">
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-12 lg:gap-16 items-center">
          {/* Left Column: Narrow Portrait Detail (4 cols) */}
          <div className="lg:col-span-5 space-y-3">
            <div className="relative h-[380px] sm:h-[460px] w-full rounded-2xl overflow-hidden border border-[#E8E1D4] shadow-xl group">
              <Image
                src="/images/destinations/coorg-coffee-detail.jpg"
                alt="Ripe coffee cherries at Rock Hills Estate, Coorg"
                fill
                sizes="(max-width: 1024px) 100vw, 40vw"
                className="object-cover object-center group-hover:scale-103 transition-transform duration-700 ease-out"
              />
              <div className="absolute inset-0 bg-gradient-to-t from-[#142820]/40 via-transparent to-transparent" />
            </div>

            <div className="flex items-center justify-between text-[11px] font-mono text-[#142820]/60 px-1">
              <span>Robusta cherries • Rock Hills Estate</span>
              <span>12.278° N, 75.712° E</span>
            </div>
          </div>

          {/* Right Column: Bodoni Display Statement & Core Belief (7 cols) */}
          <div className="lg:col-span-7 space-y-8">
            <div className="space-y-4">
              <span className="inline-flex items-center gap-2 text-xs font-semibold tracking-widest text-[#C56C4D] uppercase font-mono">
                <Sparkles className="h-3.5 w-3.5" />
                <span>The SWENA Approach</span>
              </span>

              <h2 className="font-serif text-3xl sm:text-5xl lg:text-6xl font-bold tracking-tight text-[#142820] leading-[1.08]">
                Leave room for the unexpected.
              </h2>
            </div>

            {/* Exact 48-word text */}
            <p className="font-sans text-lg sm:text-xl text-[#142820]/85 leading-relaxed font-normal max-w-2xl">
              Most travel tools force a choice between rigid tour packages and dozens of disconnected browser tabs.
              SWENA brings your driving corridors, daily stops, and budget assumptions into one clear, editable plan—so
              you spend less time coordinating and more time taking in the morning light.
            </p>

            {/* Representative Corridor Capsule */}
            <div className="rounded-2xl bg-[#E8E1D4]/70 border border-[#142820]/10 p-6 space-y-4 shadow-sm">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-xs font-bold text-[#142820] uppercase tracking-wider font-mono">
                  <Compass className="h-4 w-4 text-[#C56C4D]" />
                  <span>Representative Corridor</span>
                </div>
                <span className="text-xs font-semibold text-[#142820]/70 font-mono">Karnataka & Kerala</span>
              </div>

              <p className="font-serif text-lg font-bold text-[#142820]">
                Bengaluru → Mysuru → Coorg → Wayanad
              </p>

              <div className="grid grid-cols-3 gap-4 pt-3 border-t border-[#142820]/10 text-xs">
                <div>
                  <span className="block text-[#142820]/60 text-[11px]">Corridor Distance</span>
                  <span className="font-bold text-[#142820] text-sm">340 km</span>
                </div>
                <div>
                  <span className="block text-[#142820]/60 text-[11px]">Paced Driving Time</span>
                  <span className="font-bold text-[#142820] text-sm">~6.5 hours</span>
                </div>
                <div>
                  <span className="block text-[#142820]/60 text-[11px]">Road Profile</span>
                  <span className="font-bold text-[#142820] text-sm">Plateau to Ridge</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

"use client";

import React, { useRef } from "react";
import Link from "next/link";
import Image from "next/image";
import { ArrowRight, Compass, MapPin, Sparkles } from "lucide-react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { useGSAP } from "@gsap/react";
import { TerrainPointsCanvas } from "./TerrainPointsCanvas";

gsap.registerPlugin(ScrollTrigger);

const ROUTE_STOPS = [
  { name: "Bengaluru", distance: "0 km", elevation: "920m", role: "Origin Hub", x: 40, y: 40 },
  { name: "Mysuru", distance: "145 km", elevation: "763m", role: "Palace & Heritage", x: 100, y: 120 },
  { name: "Coorg (Madikeri)", distance: "260 km", elevation: "1,150m", role: "Coffee Country", x: 190, y: 200 },
  { name: "Wayanad", distance: "340 km", elevation: "950m", role: "Western Ghats Pass", x: 260, y: 290 },
];

export function HeroJourney() {
  const containerRef = useRef<HTMLDivElement>(null);
  const stickyStageRef = useRef<HTMLDivElement>(null);
  const heroImageWrapRef = useRef<HTMLDivElement>(null);
  const heroContentRef = useRef<HTMLDivElement>(null);
  const paperBgRef = useRef<HTMLDivElement>(null);
  const routeCardRef = useRef<HTMLDivElement>(null);
  const svgPathRef = useRef<SVGPathElement>(null);

  useGSAP(
    () => {
      const container = containerRef.current;
      const stickyStage = stickyStageRef.current;
      const heroImageWrap = heroImageWrapRef.current;
      const heroContent = heroContentRef.current;
      const paperBg = paperBgRef.current;
      const routeCard = routeCardRef.current;
      const svgPath = svgPathRef.current;

      if (!container || !stickyStage || !heroImageWrap || !heroContent || !paperBg || !routeCard) return;

      const mm = gsap.matchMedia();

      mm.add("(min-width: 900px) and (prefers-reduced-motion: no-preference)", () => {
        // Prepare SVG path length
        let pathLength = 400;
        if (svgPath) {
          pathLength = svgPath.getTotalLength();
          gsap.set(svgPath, {
            strokeDasharray: pathLength,
            strokeDashoffset: pathLength,
          });
        }

        // Initial setup
        gsap.set(paperBg, { opacity: 0 });
        gsap.set(routeCard, { autoAlpha: 0, x: 40 });
        gsap.set(heroContent, { autoAlpha: 1 });

        // Master ScrollTrigger timeline tied to 220svh container scroll
        const tl = gsap.timeline({
          scrollTrigger: {
            trigger: container,
            start: "top top",
            end: "bottom bottom",
            scrub: 0.6,
          },
        });

        // 0.00 -> 0.25: Headline fades out, paper background begins to appear
        tl.to(
          heroContent,
          {
            autoAlpha: 0,
            y: -30,
            duration: 0.25,
            ease: "power1.inOut",
          },
          0.08
        );

        tl.to(
          paperBg,
          {
            opacity: 1,
            duration: 0.35,
            ease: "power1.inOut",
          },
          0.15
        );

        // 0.20 -> 0.60: Image transitions from full-bleed to framed card on left
        tl.to(
          heroImageWrap,
          {
            top: "10vh",
            left: "4vw",
            width: "48vw",
            height: "80vh",
            borderRadius: "16px",
            boxShadow: "0 25px 60px -15px rgba(20, 40, 32, 0.35)",
            duration: 0.45,
            ease: "power2.inOut",
          },
          0.18
        );

        // 0.45 -> 0.75: Route card enters from the right
        tl.to(
          routeCard,
          {
            autoAlpha: 1,
            x: 0,
            duration: 0.3,
            ease: "power2.out",
          },
          0.45
        );

        // 0.50 -> 0.85: SVG path draws through the stops
        if (svgPath) {
          tl.to(
            svgPath,
            {
              strokeDashoffset: 0,
              duration: 0.35,
              ease: "none",
            },
            0.5
          );
        }

        // 0.70 -> 1.00: Stagger reveal for route stop nodes
        tl.fromTo(
          ".route-stop-node",
          { autoAlpha: 0, scale: 0.7 },
          { autoAlpha: 1, scale: 1, stagger: 0.08, duration: 0.25, ease: "back.out(1.5)" },
          0.65
        );

        return () => {
          tl.kill();
        };
      });

      return () => mm.revert();
    },
    { scope: containerRef }
  );

  return (
    <section
      ref={containerRef}
      className="relative w-full lg:h-[220svh] bg-[#0D1915]"
      aria-label="Scene 1: Landscape into route journey"
    >
      {/* Sticky Stage (sticks for 120svh on desktop, relative flow on mobile) */}
      <div
        ref={stickyStageRef}
        className="relative lg:sticky lg:top-0 h-auto min-h-[100svh] lg:h-[100svh] w-full overflow-hidden flex flex-col justify-between"
      >
        {/* Background Paper Surface (fades in on scroll on desktop) */}
        <div
          ref={paperBgRef}
          className="hidden lg:block absolute inset-0 bg-[#F3EFE6] z-0 pointer-events-none transition-opacity"
        />

        {/* Hero Scenic Image Wrapper */}
        <div
          ref={heroImageWrapRef}
          className="relative lg:absolute inset-0 w-full h-[85svh] lg:h-full z-10 overflow-hidden transition-all"
        >
          <Image
            src="/images/destinations/western-ghats.jpg"
            alt="Misty tea plantations and ridge lines of Kolukkumalai, Western Ghats"
            fill
            priority
            sizes="100vw"
            className="object-cover object-center"
          />

          {/* Scrim Gradients: Dark forest fading into image */}
          <div className="absolute inset-0 bg-gradient-to-r from-[#0D1915]/90 via-[#0D1915]/60 to-transparent" />
          <div className="absolute inset-0 bg-gradient-to-t from-[#0D1915] via-transparent to-[#0D1915]/40" />

          {/* Adaptive Topographic Points Canvas */}
          <TerrainPointsCanvas className="opacity-80" />

          {/* Editorial Location Stamp */}
          <div className="absolute bottom-6 left-6 sm:bottom-8 sm:left-8 z-20 flex items-center gap-2 text-[11px] text-[#F7F7F2]/80 tracking-widest uppercase font-mono bg-[#0D1915]/70 backdrop-blur-md px-3.5 py-1.5 rounded-full border border-white/10">
            <MapPin className="h-3 w-3 text-[#B7C9AD]" />
            <span>Western Ghats • 10.1167° N, 77.2333° E</span>
          </div>
        </div>

        {/* Hero Narrative Overlay (Headline & CTAs) */}
        <div
          ref={heroContentRef}
          className="absolute inset-0 z-20 flex flex-col justify-center px-6 sm:px-12 lg:px-16 pointer-events-none"
        >
          <div className="max-w-3xl pointer-events-auto pt-16 sm:pt-20">
            {/* Kicker */}
            <div className="inline-flex items-center gap-2 px-3.5 py-1 rounded-full bg-[#15271F]/80 border border-[#B7C9AD]/30 text-xs font-semibold text-[#B7C9AD] tracking-wider uppercase mb-6 backdrop-blur-md">
              <Compass className="h-3.5 w-3.5 text-[#B7C9AD]" />
              <span>Karnataka & Kerala Corridors</span>
              <span className="text-[#6E8274]">•</span>
              <span className="text-[#A9B8AD] font-normal">900m – 2,240m</span>
            </div>

            {/* Main Editorial Headline */}
            <h1 className="font-serif text-5xl sm:text-7xl lg:text-8xl font-bold tracking-tight text-[#F7F7F2] leading-[0.96] mb-6">
              Make room for <br />
              <span className="italic text-[#B7C9AD] font-normal">the journey.</span>
            </h1>

            {/* Supporting Copy */}
            <p className="text-base sm:text-xl text-[#A9B8AD] max-w-xl font-normal leading-relaxed mb-8">
              Bring your driving route, daily stops, and transparent budget assumptions into one editable trip plan.
            </p>

            {/* Actions */}
            <div className="flex flex-wrap items-center gap-4">
              <Link
                href="/dashboard"
                className="inline-flex items-center gap-2 rounded-xl bg-[#B7C9AD] px-7 py-3.5 text-sm font-semibold text-[#102D25] hover:bg-[#C9DBBE] transition-all shadow-xl shadow-black/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD] cursor-pointer"
              >
                <span>Plan my trip</span>
                <ArrowRight className="h-4 w-4" />
              </Link>

              <a
                href="#explore"
                className="inline-flex items-center gap-2 rounded-xl bg-[#15271F]/80 border border-[#233E32] px-6 py-3.5 text-sm font-medium text-[#F7F7F2] hover:bg-[#15271F] hover:border-[#B7C9AD]/40 transition-colors backdrop-blur-md cursor-pointer"
              >
                Explore journeys
              </a>
            </div>

            {/* Editorial Footer Line */}
            <div className="mt-12 pt-4 border-t border-white/10 flex items-center gap-4 text-xs text-[#A9B8AD]/70">
              <span>Direct official handoff to IRCTC, state tourism & homestays</span>
              <span>•</span>
              <span>Zero payment capture</span>
            </div>
          </div>
        </div>

        {/* Right Route Card (Revealed on Desktop Scroll) */}
        <div
          ref={routeCardRef}
          className="hidden lg:flex absolute top-[10vh] left-[56vw] w-[40vw] h-[80vh] z-20 flex-col justify-between p-8 rounded-2xl bg-[#F3EFE6] text-[#142820] border border-[#E8E1D4] shadow-2xl overflow-hidden"
        >
          <div className="space-y-3">
            <div className="flex items-center justify-between border-b border-[#142820]/10 pb-3">
              <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-[#C56C4D]">
                <Sparkles className="h-3.5 w-3.5" />
                <span>Representative Corridor</span>
              </div>
              <span className="text-xs font-mono text-[#142820]/60">340 km • 4 Stops</span>
            </div>

            <h2 className="font-serif text-2xl sm:text-3xl font-bold text-[#142820]">
              Bengaluru to Wayanad
            </h2>
            <p className="text-xs text-[#142820]/80 leading-relaxed max-w-sm font-sans">
              From the Deccan plateau down through Mysore’s heritage boulevards, ascending into the spice ridges and cool mist of Coorg and Wayanad.
            </p>
          </div>

          {/* Interactive SVG Route Schematic */}
          <div className="relative my-auto py-2">
            <svg
              viewBox="0 0 300 320"
              className="w-full h-52 stroke-[#142820]"
              fill="none"
              aria-label="Route schematic map"
            >
              {/* Background Path (Dotted) */}
              <path
                d="M 40 40 C 60 90, 80 110, 100 120 C 130 140, 160 170, 190 200 C 220 230, 240 260, 260 290"
                stroke="rgba(20, 40, 32, 0.15)"
                strokeWidth="3"
                strokeDasharray="4 4"
              />

              {/* Animated Foreground Path */}
              <path
                ref={svgPathRef}
                d="M 40 40 C 60 90, 80 110, 100 120 C 130 140, 160 170, 190 200 C 220 230, 240 260, 260 290"
                stroke="#C56C4D"
                strokeWidth="3.5"
                strokeLinecap="round"
              />

              {/* Stops Nodes */}
              {ROUTE_STOPS.map((stop, idx) => (
                <g key={stop.name} className="route-stop-node">
                  <circle cx={stop.x} cy={stop.y} r="8" fill="#F3EFE6" stroke="#C56C4D" strokeWidth="3" />
                  <circle cx={stop.x} cy={stop.y} r="3" fill="#142820" />
                  <text
                    x={stop.x + 14}
                    y={stop.y + 4}
                    className="font-sans font-bold text-[12px] fill-[#142820]"
                  >
                    {stop.name}
                  </text>
                  <text
                    x={stop.x + 14}
                    y={stop.y + 17}
                    className="font-mono text-[10px] fill-[#142820]/60"
                  >
                    {stop.distance} • {stop.elevation}
                  </text>
                </g>
              ))}
            </svg>
          </div>

          {/* Route Card Footer */}
          <div className="pt-4 border-t border-[#142820]/10 flex items-center justify-between">
            <div>
              <span className="text-[11px] font-bold text-[#142820]/70 uppercase tracking-wider block">
                Pacing Guidance
              </span>
              <span className="text-xs text-[#142820] font-medium">
                Daylight bound: 6.5 hrs driving across 3 days
              </span>
            </div>
            <Link
              href="/dashboard?corridor=western-ghats"
              className="inline-flex items-center gap-1.5 rounded-lg bg-[#142820] px-4 py-2 text-xs font-semibold text-[#F3EFE6] hover:bg-[#142820]/90 transition-all cursor-pointer"
            >
              <span>Use Corridor</span>
              <ArrowRight className="h-3 w-3" />
            </Link>
          </div>
        </div>
      </div>

      {/* Mobile-only Corridor Route Preview Card */}
      <div className="block lg:hidden px-6 py-12 bg-[#F3EFE6] text-[#142820]">
        <div className="space-y-4">
          <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-[#C56C4D]">
            <Sparkles className="h-4 w-4" />
            <span>Featured Corridor</span>
          </div>
          <h2 className="font-serif text-3xl font-bold text-[#142820]">
            Bengaluru to Wayanad
          </h2>
          <p className="text-sm text-[#142820]/80 leading-relaxed font-sans">
            340 km through the Western Ghats. Elevation climbs from 920m in Bengaluru to 1,150m across the coffee ridges of Coorg.
          </p>

          <div className="space-y-2 pt-2">
            {ROUTE_STOPS.map((s, idx) => (
              <div
                key={s.name}
                className="flex items-center justify-between p-3 rounded-xl bg-white border border-[#E8E1D4] text-xs"
              >
                <div className="flex items-center gap-2.5">
                  <span className="h-2 w-2 rounded-full bg-[#C56C4D]" />
                  <span className="font-bold text-[#142820]">{s.name}</span>
                </div>
                <span className="font-mono text-[#142820]/70">{s.distance} • {s.elevation}</span>
              </div>
            ))}
          </div>

          <div className="pt-4">
            <Link
              href="/dashboard?corridor=western-ghats"
              className="w-full inline-flex items-center justify-center gap-2 rounded-xl bg-[#142820] py-3 text-sm font-semibold text-[#F3EFE6]"
            >
              <span>Plan Western Ghats Trip</span>
              <ArrowRight className="h-4 w-4" />
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}

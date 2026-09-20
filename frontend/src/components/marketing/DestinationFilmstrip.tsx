"use client";

import React, { useRef, useState, useEffect } from "react";
import Image from "next/image";
import Link from "next/link";
import { ArrowRight, ChevronLeft, ChevronRight, Compass, MapPin } from "lucide-react";

interface CorridorSpread {
  id: string;
  slug: string;
  index: string;
  title: string;
  kicker: string;
  tagline: string;
  description: string;
  route: string;
  elevation: string;
  image: string;
  alt: string;
  ctaText: string;
}

const CORRIDORS: CorridorSpread[] = [
  {
    id: "western-ghats",
    slug: "western-ghats",
    index: "01 / 03",
    title: "Western Ghats",
    kicker: "KARNATAKA & KERALA CORRIDOR",
    tagline: "Mist, coffee country, and slower mornings.",
    description:
      "From the historic palace boulevards of Mysuru into the misty coffee estates of Madikeri and down the winding pass into Wayanad.",
    route: "Bengaluru → Mysuru → Coorg → Wayanad",
    elevation: "900m – 2,240m",
    image: "/images/destinations/western-ghats.jpg",
    alt: "Misty tea slopes and mountain ridges of Western Ghats",
    ctaText: "Plan a Ghats journey",
  },
  {
    id: "rajasthan",
    slug: "rajasthan",
    index: "02 / 03",
    title: "Through Rajasthan",
    kicker: "ROYAL RAJPUTANA CORRIDOR",
    tagline: "Courtyards, old cities, and desert light.",
    description:
      "Sunlit marble courtyards, ancient baolis, and quiet desert highways spanning the Aravalli hills between Jaipur and Jodhpur.",
    route: "Delhi → Jaipur → Pushkar → Jodhpur",
    elevation: "260m – 430m",
    image: "/images/destinations/rajasthan-courtyard.jpg",
    alt: "Amber Fort marble courtyard and stone jali in Jaipur, Rajasthan",
    ctaText: "Plan a Rajasthan journey",
  },
  {
    id: "konkan",
    slug: "konkan",
    index: "03 / 03",
    title: "Along the Konkan Coast",
    kicker: "COASTAL MAHARASHTRA & GOA",
    tagline: "Coastal roads and time by the water.",
    description:
      "Red laterite roads, ferry crossings, quiet coconut groves, and evening sea breezes stretching from Mumbai down into Goa.",
    route: "Mumbai → Alibaug → Ratnagiri → Goa",
    elevation: "Sea level – 180m",
    image: "/images/destinations/konkan-sunset.jpg",
    alt: "Golden sunset over rocky headlands and Arabian Sea along Konkan coast",
    ctaText: "Plan a coastal journey",
  },
];

export function DestinationFilmstrip() {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const [activeIndex, setActiveIndex] = useState(0);

  // Sync active index on scroll
  const handleScroll = () => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const scrollLeft = el.scrollLeft;
    const itemWidth = el.clientWidth * 0.8;
    const newIdx = Math.min(CORRIDORS.length - 1, Math.max(0, Math.round(scrollLeft / itemWidth)));
    setActiveIndex(newIdx);
  };

  const scrollToIndex = (index: number) => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const cardEl = el.children[index] as HTMLElement;
    if (cardEl) {
      cardEl.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "start" });
      setActiveIndex(index);
    }
  };

  const handlePrev = () => {
    const newIdx = Math.max(0, activeIndex - 1);
    scrollToIndex(newIdx);
  };

  const handleNext = () => {
    const newIdx = Math.min(CORRIDORS.length - 1, activeIndex + 1);
    scrollToIndex(newIdx);
  };

  return (
    <section
      id="explore"
      className="relative py-24 sm:py-32 px-6 sm:px-12 bg-[#0D1915] border-b border-[#233E32] overflow-hidden"
      aria-label="Scene 2: Destination Filmstrip"
    >
      {/* Anchor alias for backwards compatibility */}
      <span id="journeys" className="sr-only" />

      <div className="mx-auto max-w-7xl">
        {/* Editorial Section Header */}
        <div className="flex flex-col md:flex-row md:items-end justify-between mb-12 sm:mb-16 gap-6">
          <div className="max-w-2xl space-y-3">
            <span className="text-xs font-semibold tracking-widest text-[#B7C9AD] uppercase font-mono">
              Inspiration Corridors
            </span>
            <h2 className="font-serif text-3xl sm:text-5xl lg:text-6xl font-bold tracking-tight text-[#F7F7F2]">
              Find your kind of away.
            </h2>
            <p className="text-base sm:text-lg text-[#A9B8AD] max-w-xl font-normal leading-relaxed">
              Three distinct Indian landscapes, each with its own character, driving rhythm, and morning light.
            </p>
          </div>

          {/* Desktop Navigation Controls */}
          <div className="flex items-center gap-3 self-start md:self-end">
            <button
              type="button"
              onClick={handlePrev}
              disabled={activeIndex === 0}
              className="h-11 w-11 rounded-full border border-[#233E32] bg-[#15271F] text-[#F7F7F2] flex items-center justify-center hover:border-[#B7C9AD] hover:bg-[#1b3329] transition-all disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD]"
              aria-label="Previous corridor"
            >
              <ChevronLeft className="h-5 w-5" />
            </button>
            <button
              type="button"
              onClick={handleNext}
              disabled={activeIndex === CORRIDORS.length - 1}
              className="h-11 w-11 rounded-full border border-[#233E32] bg-[#15271F] text-[#F7F7F2] flex items-center justify-center hover:border-[#B7C9AD] hover:bg-[#1b3329] transition-all disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD]"
              aria-label="Next corridor"
            >
              <ChevronRight className="h-5 w-5" />
            </button>
          </div>
        </div>

        {/* Horizontal Filmstrip Track */}
        <div
          ref={scrollContainerRef}
          onScroll={handleScroll}
          className="flex gap-6 sm:gap-8 overflow-x-auto snap-x snap-mandatory pb-6 no-scrollbar -mx-6 px-6 sm:-mx-12 sm:px-12"
          role="region"
          aria-label="Corridor filmstrip carousel"
          tabIndex={0}
        >
          {CORRIDORS.map((c, idx) => (
            <article
              key={c.id}
              className="flex-shrink-0 w-[85vw] sm:w-[75vw] lg:w-[68vw] max-w-4xl snap-start rounded-3xl bg-[#15271F] border border-[#233E32] overflow-hidden shadow-2xl flex flex-col justify-between group transition-all duration-300 hover:border-[#B7C9AD]/40"
            >
              {/* Media Spread */}
              <div className="relative h-72 sm:h-96 lg:h-[420px] w-full overflow-hidden">
                <Image
                  src={c.image}
                  alt={c.alt}
                  fill
                  sizes="(max-width: 1024px) 85vw, 68vw"
                  className="object-cover object-center group-hover:scale-103 transition-transform duration-700 ease-out"
                />
                <div className="absolute inset-0 bg-gradient-to-t from-[#15271F] via-[#15271F]/20 to-transparent" />

                {/* Top Pill Badges */}
                <div className="absolute top-5 left-5 right-5 flex items-center justify-between">
                  <span className="px-3.5 py-1 rounded-full bg-[#0D1915]/80 backdrop-blur-md border border-[#B7C9AD]/25 text-xs font-semibold text-[#B7C9AD] tracking-wider uppercase font-mono">
                    {c.index} • {c.kicker}
                  </span>
                  <span className="hidden sm:inline-block px-3 py-1 rounded-full bg-[#0D1915]/70 backdrop-blur-sm border border-white/10 text-xs font-mono text-[#F7F7F2]/80">
                    {c.elevation}
                  </span>
                </div>

                {/* Tagline Overlaid at Base of Image */}
                <div className="absolute bottom-4 left-6 right-6">
                  <p className="font-serif italic text-lg sm:text-2xl text-[#F7F7F2] drop-shadow-md">
                    “{c.tagline}”
                  </p>
                </div>
              </div>

              {/* Editorial Detail & Routing Bar */}
              <div className="p-6 sm:p-8 space-y-5">
                <div className="flex flex-col sm:flex-row sm:items-baseline justify-between gap-2 border-b border-[#233E32] pb-4">
                  <h3 className="font-serif text-2xl sm:text-3xl font-bold text-[#F7F7F2]">
                    {c.title}
                  </h3>
                  <div className="flex items-center gap-2 text-xs font-mono text-[#B7C9AD]">
                    <Compass className="h-3.5 w-3.5" />
                    <span>{c.route}</span>
                  </div>
                </div>

                <p className="text-sm sm:text-base text-[#A9B8AD] leading-relaxed">
                  {c.description}
                </p>

                <div className="pt-2 flex flex-wrap items-center justify-between gap-4">
                  <Link
                    href={`/dashboard?corridor=${c.slug}`}
                    className="inline-flex items-center gap-2 rounded-xl bg-[#B7C9AD] px-6 py-3 text-xs sm:text-sm font-semibold text-[#102D25] hover:bg-[#C9DBBE] transition-all shadow-md cursor-pointer"
                  >
                    <span>{c.ctaText}</span>
                    <ArrowRight className="h-4 w-4" />
                  </Link>

                  <span className="text-xs text-[#6E8274] font-mono">
                    Official supplier handoff • Direct booking
                  </span>
                </div>
              </div>
            </article>
          ))}
        </div>

        {/* Carousel Pagination Dots */}
        <div className="mt-8 flex justify-center gap-2.5">
          {CORRIDORS.map((_, idx) => (
            <button
              key={idx}
              type="button"
              onClick={() => scrollToIndex(idx)}
              className={`h-2 rounded-full transition-all cursor-pointer ${
                activeIndex === idx ? "w-8 bg-[#B7C9AD]" : "w-2 bg-[#233E32] hover:bg-[#A9B8AD]"
              }`}
              aria-label={`Scroll to slide ${idx + 1}`}
            />
          ))}
        </div>
      </div>
    </section>
  );
}

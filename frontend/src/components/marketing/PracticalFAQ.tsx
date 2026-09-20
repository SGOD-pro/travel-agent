"use client";

import React from "react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { HelpCircle, ShieldCheck } from "lucide-react";

export function PracticalFAQ() {
  return (
    <section
      className="py-24 sm:py-32 px-6 sm:px-12 bg-[#0A1411] text-[#F7F7F2] border-b border-[#233E32]"
      aria-label="Scene 5: Practical Reassurance FAQ"
    >
      <div className="mx-auto max-w-4xl space-y-12">
        {/* Editorial Heading */}
        <div className="text-center space-y-3 max-w-xl mx-auto">
          <span className="text-xs font-semibold tracking-widest text-[#B7C9AD] uppercase font-mono">
            Practical Reassurance
          </span>
          <h2 className="font-serif text-3xl sm:text-5xl font-bold tracking-tight text-[#F7F7F2]">
            Frequently asked questions.
          </h2>
          <p className="text-sm sm:text-base text-[#A9B8AD] leading-relaxed">
            Straight answers about how SWENA plans journeys, handles pricing, and connects you to suppliers.
          </p>
        </div>

        {/* Thin dividing rules, calm accordion */}
        <Accordion type="single" collapsible className="w-full space-y-3">
          <AccordionItem
            value="faq-1"
            className="border border-[#233E32] rounded-2xl px-6 bg-[#15271F]/70 transition-colors hover:border-[#B7C9AD]/40"
          >
            <AccordionTrigger className="text-base font-semibold text-[#F7F7F2] hover:text-[#B7C9AD] py-5 text-left">
              How does SWENA generate an itinerary?
            </AccordionTrigger>
            <AccordionContent className="text-sm text-[#A9B8AD] leading-relaxed pb-5 pt-1">
              SWENA uses constraint-based optimization to sequence your daily stops, estimate transit times, and balance driving durations against daylight hours. Your brief sets the hard rules (destinations, dates, party, vehicle mode); our engine solves a Traveling Salesperson Problem with Time Windows (TSPTW) so you get a realistic daily rhythm without overlapping visits.
            </AccordionContent>
          </AccordionItem>

          <AccordionItem
            value="faq-2"
            className="border border-[#233E32] rounded-2xl px-6 bg-[#15271F]/70 transition-colors hover:border-[#B7C9AD]/40"
          >
            <AccordionTrigger className="text-base font-semibold text-[#F7F7F2] hover:text-[#B7C9AD] py-5 text-left">
              Does SWENA take payments or book tickets directly?
            </AccordionTrigger>
            <AccordionContent className="text-sm text-[#A9B8AD] leading-relaxed pb-5 pt-1">
              No. SWENA is strictly non-custodial. We hold zero customer funds and create zero tickets. When you are ready to book, we provide direct handoff links to verified official portals such as IRCTC, airlines, and registered hotel sites so you transact directly with the supplier without middleman markups.
            </AccordionContent>
          </AccordionItem>

          <AccordionItem
            value="faq-3"
            className="border border-[#233E32] rounded-2xl px-6 bg-[#15271F]/70 transition-colors hover:border-[#B7C9AD]/40"
          >
            <AccordionTrigger className="text-base font-semibold text-[#F7F7F2] hover:text-[#B7C9AD] py-5 text-left">
              Are all displayed prices live and guaranteed?
            </AccordionTrigger>
            <AccordionContent className="text-sm text-[#A9B8AD] leading-relaxed pb-5 pt-1">
              Prices on SWENA reflect their verified evidence status. Live provider quotes carry a checked timestamp and expiry window. When rates are unverified, we label them as estimates or explicit unknowns rather than inventing a fake price lock or coercing missing fees to zero.
            </AccordionContent>
          </AccordionItem>

          <AccordionItem
            value="faq-4"
            className="border border-[#233E32] rounded-2xl px-6 bg-[#15271F]/70 transition-colors hover:border-[#B7C9AD]/40"
          >
            <AccordionTrigger className="text-base font-semibold text-[#F7F7F2] hover:text-[#B7C9AD] py-5 text-left">
              Can I change stops or driving pace after creating a plan?
            </AccordionTrigger>
            <AccordionContent className="text-sm text-[#A9B8AD] leading-relaxed pb-5 pt-1">
              Yes. Every itinerary is fully editable. You can reorder stops, adjust dwell durations, change your vehicle fuel mileage assumptions, or remove destinations. SWENA recomputes only the affected travel legs and preserves your previous versions.
            </AccordionContent>
          </AccordionItem>

          <AccordionItem
            value="faq-5"
            className="border border-[#233E32] rounded-2xl px-6 bg-[#15271F]/70 transition-colors hover:border-[#B7C9AD]/40"
          >
            <AccordionTrigger className="text-base font-semibold text-[#F7F7F2] hover:text-[#B7C9AD] py-5 text-left">
              Which regions of India are currently supported?
            </AccordionTrigger>
            <AccordionContent className="text-sm text-[#A9B8AD] leading-relaxed pb-5 pt-1">
              SWENA is optimized for Indian domestic road and rail corridors, with curated topographic routing across the Western Ghats (Karnataka and Kerala), Rajasthan heritage circuits, and the Konkan coast. Additional regional circuits are added as road network datasets and elevation profiles are verified.
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </div>
    </section>
  );
}

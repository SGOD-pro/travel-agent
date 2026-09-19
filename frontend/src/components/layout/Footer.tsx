import Link from "next/link";
import { Compass, ShieldCheck, Cpu, ArrowUpRight } from "lucide-react";

export function Footer() {
  return (
    <footer className="border-t border-[#233e32] bg-[#0A1411] text-[#A9B8AD] py-16 px-6">
      <div className="mx-auto max-w-7xl">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-12 mb-16">
          {/* Col 1: Brand & Tagline */}
          <div className="md:col-span-2 space-y-4">
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-[#15271F] border border-[#B7C9AD]/20 text-[#B7C9AD]">
                <Compass className="h-5 w-5" />
              </div>
              <span className="text-xl font-bold text-[#F7F7F2]">SWENA</span>
            </div>
            <p className="text-sm leading-relaxed max-w-md text-[#A9B8AD]">
              “We gave you memory.” An India-first travel planning, comparison,
              and discovery platform. Powered by LangGraph multi-modal workflows
              and Google OR-Tools scheduling.
            </p>
            <div className="flex items-center gap-2 text-xs text-[#B7C9AD]/90 bg-[#15271F]/80 px-3.5 py-1.5 rounded-full border border-[#B7C9AD]/15 w-fit">
              <ShieldCheck className="h-3.5 w-3.5" />
              <span>Zero-Hallucination Standard: No fake quotes, non-coerced costs</span>
            </div>
          </div>

          {/* Col 2: Navigation */}
          <div>
            <h4 className="text-sm font-semibold uppercase tracking-wider text-[#F7F7F2] mb-4">
              Platform
            </h4>
            <ul className="space-y-2.5 text-sm">
              <li>
                <Link href="/" className="hover:text-[#F7F7F2] transition-colors">
                  Cinematic Showcase
                </Link>
              </li>
              <li>
                <Link href="/dashboard" className="hover:text-[#F7F7F2] transition-colors">
                  Itinerary Planner
                </Link>
              </li>
              <li>
                <Link href="/about" className="hover:text-[#F7F7F2] transition-colors">
                  About Our Ethos
                </Link>
              </li>
              <li>
                <Link href="/contact" className="hover:text-[#F7F7F2] transition-colors">
                  Concierge Inquiries
                </Link>
              </li>
            </ul>
          </div>

          {/* Col 3: Architecture & Engineering */}
          <div>
            <h4 className="text-sm font-semibold uppercase tracking-wider text-[#F7F7F2] mb-4">
              Engine & Standards
            </h4>
            <ul className="space-y-2.5 text-sm">
              <li className="flex items-center gap-1.5">
                <Cpu className="h-3.5 w-3.5 text-[#B7C9AD]" />
                <span>LangGraph Workflow State</span>
              </li>
              <li className="flex items-center gap-1.5">
                <Cpu className="h-3.5 w-3.5 text-[#B7C9AD]" />
                <span>OR-Tools TSPTW Optimization</span>
              </li>
              <li className="flex items-center gap-1.5">
                <Cpu className="h-3.5 w-3.5 text-[#B7C9AD]" />
                <span>PostGIS Spatial Authority</span>
              </li>
              <li className="flex items-center gap-1.5">
                <Cpu className="h-3.5 w-3.5 text-[#B7C9AD]" />
                <span>Upstash Redis Ephemeral Sync</span>
              </li>
            </ul>
          </div>
        </div>

        {/* Bottom bar */}
        <div className="pt-8 border-t border-[#15271F] flex flex-col sm:flex-row items-center justify-between gap-4 text-xs">
          <p>© {new Date().getFullYear()} SWENA Travel Intelligence. All rights reserved.</p>
          <div className="flex items-center gap-6">
            <span className="text-[#687d6f]">External Booking Handoff Only</span>
            <span className="text-[#687d6f]">No Internal Checkout Locks</span>
          </div>
        </div>
      </div>
    </footer>
  );
}

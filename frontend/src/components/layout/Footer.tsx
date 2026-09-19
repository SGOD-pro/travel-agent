import Link from "next/link";
import { Compass } from "lucide-react";

export function Footer() {
  return (
    <footer className="border-t border-[#233E32] bg-[#0A1411] text-[#A9B8AD] py-14 px-6 sm:px-8">
      <div className="mx-auto max-w-7xl">
        <div className="grid grid-cols-1 md:grid-cols-12 gap-10 pb-12 border-b border-[#233E32]/60">
          {/* Brand Col (6 cols) */}
          <div className="md:col-span-6 space-y-4">
            <Link href="/" className="flex items-center gap-2.5 w-fit">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[#15271F] border border-[#B7C9AD]/25 text-[#B7C9AD]">
                <Compass className="h-4 w-4" />
              </div>
              <span className="text-xl font-bold tracking-tight text-[#F7F7F2]">SWENA</span>
            </Link>
            <p className="text-sm text-[#A9B8AD] max-w-md leading-relaxed">
              India-first travel intelligence. We bring routes, daily stops, and budget
              assumptions together into one editable plan, with direct handoff to official providers.
            </p>
            <p className="text-xs text-[#6E8274]">
              “We gave you memory.”
            </p>
          </div>

          {/* Navigation Links (3 cols) */}
          <div className="md:col-span-3 space-y-3">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-[#F7F7F2]">
              Explore
            </h4>
            <ul className="space-y-2 text-sm">
              <li>
                <Link href="/#journeys" className="hover:text-[#F7F7F2] transition-colors">
                  Signature Journeys
                </Link>
              </li>
              <li>
                <Link href="/#how-it-works" className="hover:text-[#F7F7F2] transition-colors">
                  How It Works
                </Link>
              </li>
              <li>
                <Link href="/dashboard" className="hover:text-[#F7F7F2] transition-colors">
                  Trip Planner
                </Link>
              </li>
              <li>
                <Link href="/benchmarks" className="text-xs text-[#6E8274] hover:text-[#A9B8AD] transition-colors">
                  Evaluation Benchmarks
                </Link>
              </li>
            </ul>
          </div>

          {/* Company & Support (3 cols) */}
          <div className="md:col-span-3 space-y-3">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-[#F7F7F2]">
              Company
            </h4>
            <ul className="space-y-2 text-sm">
              <li>
                <Link href="/about" className="hover:text-[#F7F7F2] transition-colors">
                  About Our Approach
                </Link>
              </li>
              <li>
                <Link href="/contact" className="hover:text-[#F7F7F2] transition-colors">
                  Contact & Inquiries
                </Link>
              </li>
            </ul>
          </div>
        </div>

        {/* Bottom Bar */}
        <div className="pt-8 flex flex-col sm:flex-row items-center justify-between gap-4 text-xs text-[#6E8274]">
          <p>© {new Date().getFullYear()} SWENA. All rights reserved.</p>
          <div className="flex flex-wrap items-center gap-6">
            <span>Direct Official Supplier Handoff</span>
            <span>Non-Custodial (No Checkout Locks)</span>
          </div>
        </div>
      </div>
    </footer>
  );
}

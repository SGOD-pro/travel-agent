"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Compass, Sparkles, MapPin, ShieldCheck, User, LogIn, LogOut } from "lucide-react";
import { useAuth } from "@/components/providers/AuthProvider";

export function Navbar() {
  const pathname = usePathname();
  const { user, login, logout } = useAuth();

  const navLinks = [
    { name: "Home", href: "/" },
    { name: "Dashboard", href: "/dashboard" },
    { name: "Benchmarks", href: "/benchmarks" },
    { name: "About Us", href: "/about" },
    { name: "Contact", href: "/contact" },
  ];

  return (
    <header className="fixed top-0 left-0 right-0 z-50 transition-all duration-300">
      <div className="mx-auto max-w-7xl px-6 py-4">
        <nav className="glass-panel flex items-center justify-between rounded-2xl px-6 py-3 shadow-2xl">
          {/* Brand Logo */}
          <Link href="/" className="group flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-[#15271F] border border-[#B7C9AD]/20 text-[#B7C9AD] group-hover:scale-105 transition-transform duration-300">
              <Compass className="h-5 w-5" />
            </div>
            <div>
              <span className="text-xl font-bold tracking-tight text-[#F7F7F2]">
                SWENA
              </span>
              <span className="hidden sm:inline-block ml-2 text-xs font-medium text-[#A9B8AD] tracking-wide uppercase">
                Travel Intelligence
              </span>
            </div>
          </Link>

          {/* Navigation Links */}
          <div className="hidden md:flex items-center gap-1 bg-[#0D1915]/60 p-1.5 rounded-xl border border-[#233e32]">
            {navLinks.map((link) => {
              const isActive = pathname === link.href;
              return (
                <Link
                  key={link.name}
                  href={link.href}
                  className={`px-4 py-2 rounded-lg text-sm font-medium transition-all duration-200 ${
                    isActive
                      ? "bg-[#15271F] text-[#B7C9AD] shadow-sm"
                      : "text-[#A9B8AD] hover:text-[#F7F7F2] hover:bg-[#15271F]/40"
                  }`}
                >
                  {link.name}
                </Link>
              );
            })}
          </div>

          {/* Action CTA & Sovereign Auth */}
          <div className="flex items-center gap-3">
            {user ? (
              <div className="flex items-center gap-2">
                <div className="hidden sm:flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-[#15271F] border border-[#233e32] text-xs">
                  <User className="h-3.5 w-3.5 text-[#B7C9AD]" />
                  <span className="font-semibold text-[#F7F7F2] truncate max-w-[120px]">{user.name}</span>
                </div>
                <button
                  onClick={() => logout()}
                  className="px-3 py-1.5 rounded-xl text-xs font-semibold bg-[#15271F] text-[#A9B8AD] border border-[#233e32] hover:text-[#F7F7F2] hover:border-red-500/40 transition-colors flex items-center gap-1.5 cursor-pointer"
                  title="Sign out of SWYRA Auth"
                >
                  <LogOut className="h-3.5 w-3.5" />
                  <span className="hidden sm:inline">Sign Out</span>
                </button>
              </div>
            ) : (
              <button
                onClick={() => login()}
                className="px-3.5 py-2 rounded-xl text-xs font-semibold bg-[#15271F] text-[#B7C9AD] border border-[#B7C9AD]/30 hover:bg-[#1b3329] transition-all flex items-center gap-1.5 cursor-pointer"
                title="Sign in via SWYRA Auth (OAuth 2.1)"
              >
                <LogIn className="h-3.5 w-3.5" />
                <span>Sign In</span>
              </button>
            )}

            <Link
              href="/dashboard"
              className="btn-sage flex items-center gap-2 rounded-xl px-4 sm:px-5 py-2 sm:py-2.5 text-xs sm:text-sm font-semibold shadow-md cursor-pointer"
            >
              <Sparkles className="h-4 w-4" />
              <span>Launch Planner</span>
            </Link>
          </div>
        </nav>
      </div>
    </header>
  );
}

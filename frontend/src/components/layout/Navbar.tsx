"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Compass, Menu, User, LogIn, LogOut } from "lucide-react";
import { useAuth } from "@/components/providers/AuthProvider";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";

export function Navbar() {
  const pathname = usePathname();
  const { user, login, logout } = useAuth();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const handleScroll = () => {
      setScrolled(window.scrollY > 40);
    };
    window.addEventListener("scroll", handleScroll, { passive: true });
    return () => window.removeEventListener("scroll", handleScroll);
  }, []);

  // Close mobile drawer on route change
  useEffect(() => {
    setMobileMenuOpen(false);
  }, [pathname]);

  const navLinks = [
    { name: "Explore", href: "/#journeys" },
    { name: "How it works", href: "/#how-it-works" },
    { name: "About", href: "/about" },
    { name: "Contact", href: "/contact" },
  ];

  return (
    <>
      {/* Accessible Skip Link */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-4 focus:z-50 focus:px-4 focus:py-2 focus:bg-[#B7C9AD] focus:text-[#102D25] focus:font-semibold focus:rounded-lg focus:outline-none focus:ring-2 focus:ring-[#B7C9AD]"
      >
        Skip to main content
      </a>

      <header
        className={`fixed top-0 left-0 right-0 z-40 h-16 sm:h-20 transition-all duration-300 ${
          scrolled
            ? "bg-[#0D1915]/95 backdrop-blur-md border-b border-[#233E32]/70 shadow-lg shadow-black/20"
            : "bg-transparent border-b border-transparent"
        }`}
      >
        <div className="mx-auto max-w-7xl h-full px-5 sm:px-8 flex items-center justify-between">
          {/* Brand Wordmark */}
          <Link
            href="/"
            className="group flex items-center gap-2.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD] rounded-lg py-1 px-1.5"
            aria-label="SWENA Home"
          >
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[#15271F] border border-[#B7C9AD]/25 text-[#B7C9AD] group-hover:border-[#B7C9AD]/60 transition-colors">
              <Compass className="h-4 w-4" />
            </div>
            <span className="text-lg font-bold tracking-tight text-[#F7F7F2]">
              SWENA
            </span>
          </Link>

          {/* Desktop Navigation Links (≥ 1024px) */}
          <nav
            className="hidden lg:flex items-center gap-7"
            aria-label="Primary Navigation"
          >
            {navLinks.map((link) => {
              const isActive = pathname === link.href;
              return (
                <Link
                  key={link.name}
                  href={link.href}
                  className={`text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD] rounded-md px-2 py-1 ${
                    isActive
                      ? "text-[#F7F7F2] font-semibold"
                      : "text-[#A9B8AD] hover:text-[#F7F7F2]"
                  }`}
                  aria-current={isActive ? "page" : undefined}
                >
                  {link.name}
                </Link>
              );
            })}
          </nav>

          {/* Actions: Sign In & Primary CTA */}
          <div className="hidden lg:flex items-center gap-4">
            {user ? (
              <div className="flex items-center gap-2">
                <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-[#15271F] border border-[#233E32] text-xs text-[#F7F7F2]">
                  <User className="h-3.5 w-3.5 text-[#B7C9AD]" />
                  <span className="truncate max-w-[120px] font-medium">{user.name}</span>
                </div>
                <button
                  onClick={() => logout()}
                  className="px-2.5 py-1.5 rounded-lg text-xs font-medium text-[#A9B8AD] hover:text-[#F7F7F2] hover:bg-[#15271F] border border-transparent hover:border-[#233E32] transition-colors flex items-center gap-1 cursor-pointer"
                  title="Sign out"
                  aria-label="Sign out"
                >
                  <LogOut className="h-3.5 w-3.5" />
                </button>
              </div>
            ) : (
              <button
                onClick={() => login()}
                className="text-xs font-medium text-[#A9B8AD] hover:text-[#F7F7F2] transition-colors flex items-center gap-1.5 px-2 py-1 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD] rounded-md"
              >
                <LogIn className="h-3.5 w-3.5" />
                <span>Sign in</span>
              </button>
            )}

            <Link
              href="/dashboard"
              className="inline-flex items-center justify-center rounded-lg bg-[#B7C9AD] px-4 py-2 text-xs font-semibold text-[#102D25] hover:bg-[#C9DBBE] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD] focus-visible:ring-offset-2 focus-visible:ring-offset-[#0D1915]"
            >
              Plan my trip
            </Link>
          </div>

          {/* Mobile & Tablet Controls (< 1024px) */}
          <div className="flex lg:hidden items-center gap-3">
            <Link
              href="/dashboard"
              className="inline-flex items-center justify-center rounded-lg bg-[#B7C9AD] px-3.5 py-1.5 text-xs font-semibold text-[#102D25] hover:bg-[#C9DBBE] transition-colors"
            >
              Plan my trip
            </Link>

            <Sheet open={mobileMenuOpen} onOpenChange={setMobileMenuOpen}>
              <SheetTrigger asChild>
                <button
                  type="button"
                  className="p-2 text-[#A9B8AD] hover:text-[#F7F7F2] rounded-lg border border-[#233E32] bg-[#15271F] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#B7C9AD]"
                  aria-label="Open mobile navigation menu"
                >
                  <Menu className="h-5 w-5" />
                </button>
              </SheetTrigger>
              <SheetContent
                side="right"
                className="w-72 bg-[#0D1915] border-l border-[#233E32] p-6 text-[#F7F7F2] flex flex-col justify-between"
              >
                <div>
                  <SheetHeader className="text-left pb-6 border-b border-[#233E32]/60">
                    <SheetTitle className="flex items-center gap-2 text-base font-bold text-[#F7F7F2]">
                      <Compass className="h-4 w-4 text-[#B7C9AD]" />
                      <span>SWENA</span>
                    </SheetTitle>
                  </SheetHeader>

                  <nav className="flex flex-col gap-4 py-6" aria-label="Mobile Navigation">
                    {navLinks.map((link) => (
                      <Link
                        key={link.name}
                        href={link.href}
                        onClick={() => setMobileMenuOpen(false)}
                        className="text-base font-medium text-[#A9B8AD] hover:text-[#F7F7F2] transition-colors py-1.5"
                      >
                        {link.name}
                      </Link>
                    ))}
                  </nav>
                </div>

                <div className="pt-6 border-t border-[#233E32]/60 space-y-4">
                  {user ? (
                    <div className="space-y-3">
                      <div className="text-xs text-[#A9B8AD]">
                        Signed in as <span className="text-[#F7F7F2] font-medium">{user.name}</span>
                      </div>
                      <button
                        onClick={() => {
                          setMobileMenuOpen(false);
                          logout();
                        }}
                        className="w-full text-left text-xs text-red-400 hover:text-red-300 py-1"
                      >
                        Sign out
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => {
                        setMobileMenuOpen(false);
                        login();
                      }}
                      className="w-full text-left text-sm font-medium text-[#B7C9AD] hover:text-[#C9DBBE] py-1"
                    >
                      Sign in with SWYRA
                    </button>
                  )}

                  <Link
                    href="/dashboard"
                    onClick={() => setMobileMenuOpen(false)}
                    className="block w-full text-center rounded-lg bg-[#B7C9AD] py-2.5 text-sm font-semibold text-[#102D25] hover:bg-[#C9DBBE] transition-colors"
                  >
                    Plan my trip
                  </Link>
                </div>
              </SheetContent>
            </Sheet>
          </div>
        </div>
      </header>
    </>
  );
}

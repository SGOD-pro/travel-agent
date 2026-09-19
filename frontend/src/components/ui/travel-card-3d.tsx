"use client";

import React, { useRef } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import { ArrowUpRight, Compass, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface TravelCard3DProps {
  title: string;
  subtitle: string;
  tagline?: string;
  badge?: string;
  verified?: boolean;
  imageUrl?: string;
  accentColor?: string;
  actionText?: string;
  onActionClick?: () => void;
  className?: string;
}

export function TravelCard3D({
  title,
  subtitle,
  tagline,
  badge = "Scenic Corridor",
  verified = true,
  imageUrl,
  actionText = "Explore Corridor",
  onActionClick,
  className,
}: TravelCard3DProps) {
  const cardRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const badgeRef = useRef<HTMLDivElement>(null);
  const ctaRef = useRef<HTMLDivElement>(null);

  useGSAP(
    () => {
      const card = cardRef.current;
      if (!card) return;

      const mm = gsap.matchMedia();

      mm.add("(prefers-reduced-motion: no-preference)", () => {
        const handleMouseMove = (e: MouseEvent) => {
          const rect = card.getBoundingClientRect();
          const x = e.clientX - rect.left;
          const y = e.clientY - rect.top;
          const centerX = rect.width / 2;
          const centerY = rect.height / 2;

          const rotateX = ((y - centerY) / centerY) * -12;
          const rotateY = ((x - centerX) / centerX) * 12;

          gsap.to(card, {
            rotationX: rotateX,
            rotationY: rotateY,
            transformPerspective: 1000,
            transformStyle: "preserve-3d",
            duration: 0.35,
            ease: "power2.out",
            overwrite: "auto",
          });

          if (badgeRef.current) {
            gsap.to(badgeRef.current, {
              z: 35,
              duration: 0.35,
              ease: "power2.out",
              overwrite: "auto",
            });
          }

          if (ctaRef.current) {
            gsap.to(ctaRef.current, {
              z: 50,
              scale: 1.02,
              duration: 0.35,
              ease: "power2.out",
              overwrite: "auto",
            });
          }
        };

        const handleMouseLeave = () => {
          gsap.to(card, {
            rotationX: 0,
            rotationY: 0,
            duration: 0.6,
            ease: "power3.out",
            overwrite: "auto",
          });

          if (badgeRef.current) {
            gsap.to(badgeRef.current, {
              z: 0,
              duration: 0.6,
              ease: "power3.out",
              overwrite: "auto",
            });
          }

          if (ctaRef.current) {
            gsap.to(ctaRef.current, {
              z: 0,
              scale: 1,
              duration: 0.6,
              ease: "power3.out",
              overwrite: "auto",
            });
          }
        };

        card.addEventListener("mousemove", handleMouseMove);
        card.addEventListener("mouseleave", handleMouseLeave);

        return () => {
          card.removeEventListener("mousemove", handleMouseMove);
          card.removeEventListener("mouseleave", handleMouseLeave);
        };
      });

      return () => mm.revert();
    },
    { scope: cardRef }
  );

  return (
    <div
      ref={cardRef}
      style={{
        transformStyle: "preserve-3d",
        perspective: 1000,
      }}
      className={cn(
        "group relative h-[360px] w-full max-w-[380px] rounded-2xl p-6",
        "bg-gradient-to-b from-[#15271F] to-[#0D1915]",
        "border border-[#233e32] hover:border-[#B7C9AD]/40",
        "shadow-xl hover:shadow-2xl hover:shadow-[#B7C9AD]/10",
        "transition-shadow duration-500 cursor-pointer overflow-hidden",
        className
      )}
      onClick={onActionClick}
    >
      {/* Background ambient light layer */}
      <div className="absolute inset-0 bg-radial from-[#B7C9AD]/5 to-transparent pointer-events-none opacity-0 group-hover:opacity-100 transition-opacity duration-500" />

      {/* Decorative image fallback if provided */}
      {imageUrl && (
        <div className="absolute inset-0 z-0 opacity-20 group-hover:opacity-30 transition-opacity duration-500">
          <img src={imageUrl} alt={title} className="h-full w-full object-cover" />
          <div className="absolute inset-0 bg-gradient-to-t from-[#0D1915] via-[#15271F]/80 to-transparent" />
        </div>
      )}

      {/* Card Content */}
      <div
        ref={contentRef}
        style={{ transform: "translateZ(20px)" }}
        className="relative z-10 flex h-full flex-col justify-between"
      >
        {/* Top bar: Badge and Verified status */}
        <div className="flex items-center justify-between">
          <div ref={badgeRef} style={{ transformStyle: "preserve-3d" }}>
            <Badge
              variant="outline"
              className="bg-[#0D1915]/80 border-[#B7C9AD]/30 text-[#B7C9AD] text-xs font-medium px-2.5 py-1 tracking-wider"
            >
              <Compass className="mr-1.5 h-3.5 w-3.5" />
              {badge}
            </Badge>
          </div>
          {verified && (
            <div className="flex items-center gap-1 text-xs text-[#A9B8AD] bg-[#15271F]/80 px-2 py-0.5 rounded-full border border-[#233e32]">
              <ShieldCheck className="h-3 w-3 text-[#B7C9AD]" />
              <span>Ground Truth</span>
            </div>
          )}
        </div>

        {/* Middle content: Title & Subtitle */}
        <div className="space-y-2 my-auto">
          <h3 className="text-xl font-semibold tracking-tight text-[#F7F7F2] group-hover:text-[#B7C9AD] transition-colors">
            {title}
          </h3>
          <p className="text-sm text-[#A9B8AD] leading-relaxed">{subtitle}</p>
          {tagline && (
            <p className="text-xs text-[#B7C9AD]/80 font-mono tracking-wide">
              {tagline}
            </p>
          )}
        </div>

        {/* Bottom bar: Action button */}
        <div ref={ctaRef} style={{ transformStyle: "preserve-3d" }}>
          <Button
            size="sm"
            className="w-full bg-[#B7C9AD] hover:bg-[#c9dbbe] text-[#102D25] font-semibold flex items-center justify-between shadow-md"
            onClick={(e) => {
              e.stopPropagation();
              onActionClick?.();
            }}
          >
            <span>{actionText}</span>
            <ArrowUpRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5 group-hover:-translate-y-0.5" />
          </Button>
        </div>
      </div>
    </div>
  );
}

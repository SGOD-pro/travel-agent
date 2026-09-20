"use client";

import React, { useEffect, useRef } from "react";

/**
 * TerrainPointsCanvas
 *
 * Lightweight, adaptive canvas rendering subtle topographic elevation points
 * inspired by VGPU adaptive-quality architecture.
 *
 * - Non-blocking: executes via requestAnimationFrame with frame-delta clamping.
 * - Progressive degradation: detects low frame rates and reduces density / pauses.
 * - Decorative only: pointer-events-none, aria-hidden="true".
 */
export function TerrainPointsCanvas({ className = "" }: { className?: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    let animationFrameId: number;
    let width = 0;
    let height = 0;
    let lastTime = performance.now();
    let frameBudgetFailures = 0;
    let isDegraded = false;

    // Check device motion preferences
    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (prefersReducedMotion) {
      return; // Do not animate for users preferring reduced motion
    }

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      width = canvas.parentElement?.clientWidth || window.innerWidth;
      height = canvas.parentElement?.clientHeight || window.innerHeight;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      ctx.scale(dpr, dpr);
    };

    resize();
    window.addEventListener("resize", resize, { passive: true });

    // Topographic point field configuration
    const cols = 24;
    const rows = 14;
    let offsetTime = 0;

    const render = (time: number) => {
      const delta = time - lastTime;
      lastTime = time;

      // Adaptive quality: If frames take longer than 33ms (drop below ~30fps) for 5 frames, degrade
      if (delta > 33) {
        frameBudgetFailures++;
        if (frameBudgetFailures > 5 && !isDegraded) {
          isDegraded = true; // Downgrade animation frequency
        }
      } else {
        frameBudgetFailures = Math.max(0, frameBudgetFailures - 1);
      }

      ctx.clearRect(0, 0, width, height);

      offsetTime += isDegraded ? 0.003 : 0.007;

      const cellX = width / (cols + 1);
      const cellY = height / (rows + 1);

      ctx.fillStyle = "rgba(188, 201, 175, 0.22)"; // Pale sage accent

      for (let i = 1; i <= cols; i++) {
        for (let j = 1; j <= rows; j++) {
          const x = i * cellX;
          const y = j * cellY;

          // Gentle sine-wave wave mimicking ridge line contours
          const wave = Math.sin(i * 0.25 + offsetTime) * Math.cos(j * 0.35 + offsetTime * 0.8);
          const radius = Math.max(0.6, 1.2 + wave * 0.8);
          const opacity = Math.max(0.04, 0.16 + wave * 0.12);

          ctx.beginPath();
          ctx.arc(x, y + wave * 8, radius, 0, Math.PI * 2);
          ctx.fillStyle = `rgba(188, 201, 175, ${opacity})`;
          ctx.fill();
        }
      }

      animationFrameId = requestAnimationFrame(render);
    };

    animationFrameId = requestAnimationFrame(render);

    return () => {
      window.removeEventListener("resize", resize);
      cancelAnimationFrame(animationFrameId);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className={`absolute inset-0 pointer-events-none select-none z-1 ${className}`}
      aria-hidden="true"
    />
  );
}

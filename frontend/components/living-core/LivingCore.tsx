"use client";

import { useEffect, useRef, useSyncExternalStore } from "react";

// The Living Core is MainAI's visual identity and nothing else. It deliberately imports no API
// client, no auth, no job/approval types and no security code: it cannot decide anything, and a
// rendering failure cannot leak into the cognitive or security layers. Callers pass a state that
// THEY derived from real system events (see app/(shell)/page.tsx and chat/page.tsx).
//
// "Emotional" modes are presentation, not claims about the founder or about MainAI's inner life.
// The component is decorative (aria-hidden canvas + a factual text alternative), so a screen
// reader hears the operational state its parent already knows, never an inferred feeling.

export type CoreState =
  | "resting"
  | "listening"
  | "reasoning"
  | "creating"
  | "speaking"
  | "security"
  | "degraded";

export type CoreMode = "harmony" | "care" | "creativity" | "curiosity" | "focus" | "protection";

type RGB = readonly [number, number, number];

type Palette = {
  core: RGB;
  inner: RGB;
  outer: RGB;
  accent: RGB;
};

// Palette values are intentional low-saturation: the Core should read as ambient light, not as
// a status LED that could be mistaken for a verified health/security signal.
const STATE_PALETTES: Record<CoreState, Palette> = {
  resting: { core: [124, 92, 255], inner: [92, 120, 255], outer: [60, 224, 255], accent: [168, 148, 255] },
  listening: { core: [52, 211, 153], inner: [56, 189, 248], outer: [124, 92, 255], accent: [167, 243, 208] },
  reasoning: { core: [168, 85, 247], inner: [124, 92, 255], outer: [60, 224, 255], accent: [216, 180, 254] },
  creating: { core: [60, 224, 255], inner: [124, 92, 255], outer: [236, 72, 153], accent: [165, 243, 252] },
  speaking: { core: [96, 165, 250], inner: [60, 224, 255], outer: [124, 92, 255], accent: [191, 219, 254] },
  security: { core: [148, 163, 184], inner: [94, 234, 212], outer: [124, 92, 255], accent: [203, 213, 225] },
  degraded: { core: [148, 120, 90], inner: [120, 113, 108], outer: [90, 90, 110], accent: [214, 180, 140] },
};

const MODE_TINT: Record<CoreMode, RGB> = {
  harmony: [124, 92, 255],
  care: [244, 164, 180],
  creativity: [236, 72, 153],
  curiosity: [60, 224, 255],
  focus: [96, 165, 250],
  protection: [94, 234, 212],
};

type Motion = {
  speed: number;
  amplitude: number;
  bloom: number;
  layers: number;
  trail: number;
};

const STATE_MOTION: Record<CoreState, Motion> = {
  resting: { speed: 0.16, amplitude: 0.055, bloom: 0.3, layers: 4, trail: 0.1 },
  listening: { speed: 0.62, amplitude: 0.085, bloom: 0.42, layers: 5, trail: 0.14 },
  reasoning: { speed: 0.9, amplitude: 0.07, bloom: 0.36, layers: 5, trail: 0.16 },
  creating: { speed: 0.5, amplitude: 0.11, bloom: 0.5, layers: 5, trail: 0.2 },
  speaking: { speed: 1.5, amplitude: 0.09, bloom: 0.55, layers: 5, trail: 0.12 },
  security: { speed: 0.22, amplitude: 0.04, bloom: 0.28, layers: 4, trail: 0.08 },
  degraded: { speed: 0.1, amplitude: 0.025, bloom: 0.18, layers: 3, trail: 0.06 },
};

const PHASES = [0, 1.7, 3.1, 4.6, 5.9];
const ORBITS = [0.34, 0.62, 0.48, 0.78, 0.56];

function lerp(a: number, b: number, t: number) {
  return a + (b - a) * t;
}

function lerpRGB(a: RGB, b: RGB, t: number): RGB {
  return [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
}

function rgba(c: RGB, alpha: number) {
  return `rgba(${Math.round(c[0])}, ${Math.round(c[1])}, ${Math.round(c[2])}, ${alpha.toFixed(3)})`;
}

function prefersReducedMotion() {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

function isLowPower() {
  if (typeof navigator === "undefined") return false;
  const cores = navigator.hardwareConcurrency ?? 8;
  const memory = (navigator as Navigator & { deviceMemory?: number }).deviceMemory ?? 8;
  return cores <= 4 || memory <= 4;
}

export default function LivingCore({
  state = "resting",
  mode = "harmony",
  size = 240,
  className = "",
}: {
  state?: CoreState;
  mode?: CoreMode;
  size?: number;
  className?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // Subscribed, not copied into state from an effect: the server snapshot is always "full",
  // while the client snapshot reflects the real preference at first render. Playwright can
  // emulate the preference before navigation, so the reduced-motion path is testable.
  const motionMode = useSyncExternalStore<"full" | "reduced">(
    (onChange) => {
      if (typeof window === "undefined" || !window.matchMedia) return () => undefined;
      const query = window.matchMedia("(prefers-reduced-motion: reduce)");
      query.addEventListener("change", onChange);
      return () => query.removeEventListener("change", onChange);
    },
    () => (prefersReducedMotion() ? "reduced" : "full"),
    () => "full"
  );
  // Refs, not React state: the animation loop reads the latest requested state without
  // restarting (and without re-allocating gradients) on every parent render.
  const stateRef = useRef(state);
  const modeRef = useRef(mode);
  const afterglowRef = useRef(0);
  const previousStateRef = useRef(state);

  useEffect(() => {
    stateRef.current = state;
    if (previousStateRef.current !== state) {
      // Gentle afterglow instead of an abrupt reset: the previous state's colours remain as a
      // decaying halo while the new one fades in. Presentation only; no event is emitted.
      afterglowRef.current = 1;
      previousStateRef.current = state;
    }
    modeRef.current = mode;
  }, [state, mode]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduced = motionMode === "reduced";
    const lowPower = isLowPower();
    const dpr = Math.min(window.devicePixelRatio || 1, reduced || lowPower ? 1 : 2);
    let width = 0;
    let height = 0;

    const resize = () => {
      width = canvas.clientWidth || size;
      height = canvas.clientHeight || size;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (reduced) drawFrame(0);
    };

    // Eased values so a state change glides over ~1.2 s rather than snapping.
    let motion = { ...STATE_MOTION[stateRef.current] };
    let palette: Palette = { ...STATE_PALETTES[stateRef.current] };
    const ease = reduced ? 1 : 0.035;

    const drawFrame = (elapsed: number) => {
      if (!width || !height) return;
      const targetMotion = STATE_MOTION[stateRef.current];
      const targetPalette = STATE_PALETTES[stateRef.current];
      motion.speed = lerp(motion.speed, targetMotion.speed, ease);
      motion.amplitude = lerp(motion.amplitude, targetMotion.amplitude, ease);
      motion.bloom = lerp(motion.bloom, targetMotion.bloom, ease);
      motion.trail = lerp(motion.trail, targetMotion.trail, ease);
      motion.layers = Math.round(lerp(motion.layers, targetMotion.layers, ease));
      palette = {
        core: lerpRGB(palette.core, targetPalette.core, ease),
        inner: lerpRGB(palette.inner, targetPalette.inner, ease),
        outer: lerpRGB(palette.outer, targetPalette.outer, ease),
        accent: lerpRGB(palette.accent, targetPalette.accent, ease),
      };

      const tint = MODE_TINT[modeRef.current];
      const glow = afterglowRef.current;
      afterglowRef.current = reduced ? 0 : Math.max(0, glow - 0.012);

      const cx = width / 2;
      const cy = height / 2;
      const base = Math.min(width, height) * 0.5;
      const t = elapsed / 1000;
      const breathe = 1 + Math.sin(t * (0.6 + motion.speed)) * 0.02;

      // Trail: a translucent wash produces the afterglow and smooths transitions. Reduced
      // motion clears fully so no frame-to-frame movement is implied.
      ctx.globalCompositeOperation = "source-over";
      if (motion.trail > 0) {
        ctx.fillStyle = `rgba(5, 5, 10, ${Math.max(0.06, motion.trail)})`;
        ctx.fillRect(0, 0, width, height);
      } else {
        ctx.clearRect(0, 0, width, height);
      }

      ctx.globalCompositeOperation = "lighter";
      const layers = Math.max(3, Math.min(motion.layers, lowPower ? 3 : 5));
      for (let i = 0; i < layers; i++) {
        const phase = PHASES[i % PHASES.length];
        const orbit = ORBITS[i % ORBITS.length];
        const drift = reduced ? 0 : Math.sin(t * (0.18 + motion.speed * (0.35 + i * 0.08)) + phase);
        const drift2 = reduced ? 0 : Math.cos(t * (0.13 + motion.speed * 0.22) + phase * 1.3);
        const x = cx + drift * base * orbit * motion.amplitude * 6;
        const y = cy + drift2 * base * orbit * motion.amplitude * 5;
        const radius = base * (0.36 + i * 0.1) * (reduced ? 1 : 1 + drift * motion.amplitude) * breathe;
        const color = i % 2 === 0 ? palette.inner : palette.outer;
        const mixed = lerpRGB(color, tint, 0.28);
        const alpha = (0.2 - i * 0.022) * (1 + motion.bloom) + glow * 0.02;

        const gradient = ctx.createRadialGradient(x, y, radius * 0.08, x, y, radius);
        gradient.addColorStop(0, rgba(mixed, Math.max(0.02, alpha)));
        gradient.addColorStop(0.55, rgba(mixed, Math.max(0.01, alpha * 0.45)));
        gradient.addColorStop(1, rgba(mixed, 0));
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(x, y, radius, 0, Math.PI * 2);
        ctx.fill();
      }

      // Central core — the only element that pulses at rest.
      const coreRadius = base * 0.3 * breathe;
      const coreGradient = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreRadius * 1.8);
      coreGradient.addColorStop(0, rgba(palette.accent, 0.5 + glow * 0.15));
      coreGradient.addColorStop(0.35, rgba(palette.core, 0.28));
      coreGradient.addColorStop(1, rgba(palette.core, 0));
      ctx.fillStyle = coreGradient;
      ctx.beginPath();
      ctx.arc(cx, cy, coreRadius * 1.8, 0, Math.PI * 2);
      ctx.fill();

      // A single slow halo, drawn only for states that represent active work (creating,
      // reasoning, security). It visualises that activity is ongoing; it is not a claim about
      // progress or protection. Resting/degraded have no ring.
      const activeRing = stateRef.current === "creating" || stateRef.current === "reasoning" || stateRef.current === "security";
      if (activeRing && !reduced) {
        const ringRadius = base * (0.66 + Math.sin(t * 0.35) * 0.015);
        ctx.strokeStyle = rgba(palette.accent, 0.14);
        ctx.lineWidth = Math.max(1, base * 0.006);
        ctx.beginPath();
        ctx.arc(cx, cy, ringRadius, 0, Math.PI * 2);
        ctx.stroke();
      }

      ctx.globalCompositeOperation = "source-over";
    };

    resize();
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(resize) : null;
    observer?.observe(canvas);

    let raf = 0;
    let last = 0;
    let elapsed = 0;
    const frameBudget = 1000 / (lowPower ? 20 : 30);
    const start = performance.now();

    const loop = (now: number) => {
      raf = requestAnimationFrame(loop);
      if (document.hidden) return; // no frames while the tab is hidden: battery and CPU.
      if (now - last < frameBudget) return;
      last = now;
      elapsed = now - start;
      drawFrame(elapsed);
    };

    if (reduced) {
      drawFrame(0);
    } else {
      raf = requestAnimationFrame(loop);
    }

    const onVisibility = () => {
      if (!document.hidden && !reduced) {
        last = 0; // resume without a catch-up jump
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      cancelAnimationFrame(raf);
      observer?.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [size, motionMode]);

  return (
    <div className={`relative inline-flex items-center justify-center ${className}`}>
      <canvas
        ref={canvasRef}
        width={size}
        height={size}
        style={{ width: size, height: size }}
        className="rounded-full"
        data-testid="living-core"
        data-core-state={state}
        data-core-mode={mode}
        data-motion={motionMode}
        aria-hidden="true"
      />
      <div className="pointer-events-none absolute inset-0 rounded-full ring-1 ring-white/5" aria-hidden="true" />
    </div>
  );
}

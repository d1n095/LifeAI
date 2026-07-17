"use client";

export type OrbState = "idle" | "listening" | "thinking" | "speaking" | "error";

const STATE_CONFIG: Record<OrbState, { gradient: string; animation: string; label: string; glow: string }> = {
  idle: { gradient: "from-accent via-accent2 to-accent", animation: "animate-orb-idle", label: "Vilar", glow: "124,92,255" },
  listening: {
    gradient: "from-emerald-400 via-emerald-300 to-cyan-400",
    animation: "animate-orb-listening",
    label: "Lyssnar",
    glow: "52,211,153",
  },
  thinking: {
    gradient: "from-accent via-fuchsia-400 to-accent2",
    animation: "animate-orb-thinking",
    label: "Tänker",
    glow: "168,85,247",
  },
  speaking: {
    gradient: "from-accent2 via-sky-300 to-accent",
    animation: "animate-orb-speaking",
    label: "Talar",
    glow: "60,224,255",
  },
  error: { gradient: "from-red-500 via-red-400 to-red-600", animation: "animate-orb-error", label: "Fel", glow: "239,68,68" },
};

export default function Orb({ state = "idle", size = 160 }: { state?: OrbState; size?: number }) {
  const config = STATE_CONFIG[state];

  return (
    <div className="flex flex-col items-center gap-3">
      <div
        className={`rounded-full bg-gradient-to-br ${config.gradient} ${config.animation}`}
        style={{
          width: size,
          height: size,
          boxShadow: `0 0 ${Math.round(size / 2.2)}px rgba(${config.glow}, 0.45)`,
        }}
        aria-hidden="true"
      />
      {/* Screen readers get the state change announced without visually duplicating the label */}
      <div role="status" aria-live="polite" className="text-xs text-white/40">
        <span aria-hidden="true">{config.label}</span>
        <span className="sr-only">MainAI-status: {config.label}</span>
      </div>
    </div>
  );
}

import { Confidence } from "@/lib/api";

const CONFIG: Record<Confidence, { label: string; className: string; description: string }> = {
  high: {
    label: "Hög tillförlitlighet",
    className: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
    description: "Svaret bygger på källor med stark matchning mot frågan.",
  },
  medium: {
    label: "Måttlig tillförlitlighet",
    className: "bg-amber-500/15 text-amber-300 border-amber-500/30",
    description: "Underlaget är begränsat — delar av svaret kan vara osäkra.",
  },
  low: {
    label: "Låg tillförlitlighet",
    className: "bg-orange-500/15 text-orange-300 border-orange-500/30",
    description: "Svagt underlag i kunskapsbiblioteket. Verifiera innan du litar på svaret.",
  },
  none: {
    label: "Inget underlag hittat",
    className: "bg-red-500/15 text-red-300 border-red-500/30",
    description: "Ingen relevant källa hittades — svaret bygger inte på företagets kunskap.",
  },
};

export default function ConfidenceBadge({ confidence, score }: { confidence: Confidence; score: number }) {
  const config = CONFIG[confidence];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${config.className}`}
      title={config.description}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
      {config.label}
      <span className="text-current/70">({score.toFixed(2)})</span>
    </span>
  );
}

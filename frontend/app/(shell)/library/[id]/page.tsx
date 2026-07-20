"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { api, KnowledgeSourceDetail, KnowledgeSourceItem } from "@/lib/api";

const STATUS_LABELS: Record<string, string> = {
  active: "Aktiv",
  historical: "Historisk",
  proposed: "Förslag",
  superseded: "Ersatt",
  disputed: "Omtvistad",
};

const RELATIONSHIP_LABELS: Record<string, string> = {
  derived_from: "härrör från",
  supersedes: "ersätter",
  contradicts: "motsäger",
  supports: "stödjer",
  duplicates: "är en dubblett av",
  belongs_to: "tillhör",
};

const RELATIONSHIP_TYPES = Object.keys(RELATIONSHIP_LABELS);

const CLAIM_CONFIDENCE_LABELS: Record<string, string> = {
  certain: "Säkerställt",
  likely: "Troligt",
  uncertain: "Osäkert",
  conflict: "Konflikt",
  no_basis: "Inget underlag",
};

const CLAIM_CONFIDENCE_COLORS: Record<string, string> = {
  certain: "bg-emerald-500/20 text-emerald-300",
  likely: "bg-sky-500/20 text-sky-300",
  uncertain: "bg-amber-500/20 text-amber-300",
  conflict: "bg-red-500/20 text-red-300",
  no_basis: "bg-white/10 text-white/50",
};

export default function LibrarySourceDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [source, setSource] = useState<KnowledgeSourceDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const [otherSources, setOtherSources] = useState<KnowledgeSourceItem[]>([]);
  const [relTarget, setRelTarget] = useState("");
  const [relType, setRelType] = useState("supports");
  const [relNote, setRelNote] = useState("");
  const [relError, setRelError] = useState<string | null>(null);
  const [addingRelationship, setAddingRelationship] = useState(false);

  async function refresh() {
    try {
      setError(null);
      setSource(await api.getLibrarySource(params.id));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    api
      .listLibrary()
      .then((all) => setOtherSources(all.filter((s) => s.id !== params.id)))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.id]);

  async function handleDelete() {
    setDeleting(true);
    setError(null);
    try {
      await api.deleteLibrarySource(params.id);
      router.replace("/library");
    } catch (e: any) {
      setError(e.message);
      setDeleting(false);
    }
  }

  async function handleAddRelationship(e: React.FormEvent) {
    e.preventDefault();
    if (!relTarget) return;
    setAddingRelationship(true);
    setRelError(null);
    try {
      await api.createSourceRelationship(params.id, relTarget, relType, relNote || undefined);
      setRelTarget("");
      setRelNote("");
      await refresh();
    } catch (e: any) {
      setRelError(e.message);
    } finally {
      setAddingRelationship(false);
    }
  }

  if (loading) {
    return (
      <div className="text-white/40 text-sm" role="status">
        Laddar…
      </div>
    );
  }

  if (error && !source) {
    return (
      <div className="space-y-4">
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
        <Link href="/library" className="text-sm text-accent2 hover:underline">
          ← Tillbaka till biblioteket
        </Link>
      </div>
    );
  }

  if (!source) return null;

  return (
    <div className="space-y-6 max-w-3xl">
      <Link href="/library" className="text-sm text-white/50 hover:text-white">
        ← Tillbaka till biblioteket
      </Link>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      <div>
        <h1 className="text-xl font-semibold">{source.title}</h1>
        <p className="text-white/50 text-sm mt-1">
          {source.original_filename} · {source.media_type || "okänd filtyp"}
        </p>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <Stat label="Status" value={STATUS_LABELS[source.active_truth_status]} />
        <Stat label="Klassificering" value={source.classification} />
        <Stat label="Indexering" value={`${source.status} (${source.chunk_count} chunkar)`} />
        <Stat label="Version" value={`v${source.version_number}`} />
      </div>

      {source.error_message && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
          Indexeringsfel: {source.error_message}
        </div>
      )}

      <section aria-labelledby="preview-heading" className="rounded-xl border border-border p-4">
        <h2 id="preview-heading" className="text-sm font-medium text-white/70 mb-3">
          Förhandsvisning
        </h2>
        {source.chunk_preview.length === 0 && <p className="text-white/30 text-sm">Inget innehåll indexerat ännu.</p>}
        <div className="space-y-2">
          {source.chunk_preview.map((chunk, i) => (
            <p key={i} className="text-sm text-white/60 border-b border-border/60 pb-2 last:border-0">
              {chunk}…
            </p>
          ))}
        </div>
      </section>

      <section aria-labelledby="claims-heading" className="rounded-xl border border-border p-4">
        <h2 id="claims-heading" className="text-sm font-medium text-white/70 mb-3">
          Sakpåståenden (claims)
        </h2>
        {source.claims.length === 0 && (
          <p className="text-white/30 text-sm">Inga sakpåståenden extraherade ännu.</p>
        )}
        <ul className="space-y-2">
          {source.claims.map((c) => (
            <li key={c.id} className="text-sm border-b border-border/60 pb-2 last:border-0">
              <div className="flex items-start justify-between gap-2">
                <span className="text-white/80">{c.claim_text}</span>
                <span className={`shrink-0 rounded px-2 py-0.5 text-[10px] ${CLAIM_CONFIDENCE_COLORS[c.confidence]}`}>
                  {CLAIM_CONFIDENCE_LABELS[c.confidence] || c.confidence}
                </span>
              </div>
              <div className="mt-1 text-[11px] text-white/30">
                {STATUS_LABELS[c.status] || c.status} · underlag {(c.grounding_score * 100).toFixed(0)}%
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="versions-heading" className="rounded-xl border border-border p-4">
        <h2 id="versions-heading" className="text-sm font-medium text-white/70 mb-3">
          Versionshistorik
        </h2>
        {source.versions.length === 0 && <p className="text-white/30 text-sm">Ingen versionshistorik.</p>}
        <ul className="space-y-1">
          {source.versions.map((v) => (
            <li key={v.id} className="text-sm text-white/60 flex justify-between border-b border-border/60 py-2 last:border-0">
              <span>
                v{v.version_number} · {v.extraction_version}
              </span>
              <span className="text-white/30">{new Date(v.created_at).toLocaleString("sv-SE")}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="relationships-heading" className="rounded-xl border border-border p-4">
        <h2 id="relationships-heading" className="text-sm font-medium text-white/70 mb-3">
          Relationer till andra källor
        </h2>
        {source.relationships.length === 0 && <p className="text-white/30 text-sm mb-4">Inga relationer registrerade.</p>}
        <ul className="space-y-1 mb-4">
          {source.relationships.map((r) => {
            const isFrom = r.from_source_id === source.id;
            const otherId = isFrom ? r.to_source_id : r.from_source_id;
            return (
              <li key={r.id} className="text-sm text-white/60 border-b border-border/60 py-2 last:border-0">
                {isFrom ? "Denna källa" : "Källan"}{" "}
                <span className="text-accent2">{RELATIONSHIP_LABELS[r.relationship_type] || r.relationship_type}</span>{" "}
                <Link href={`/library/${otherId}`} className="underline-offset-2 hover:underline text-white/80">
                  {isFrom ? "en annan källa" : "denna källa"} ({otherId.slice(0, 8)}…)
                </Link>
                {r.note && <span className="text-white/40"> — {r.note}</span>}
              </li>
            );
          })}
        </ul>

        <form onSubmit={handleAddRelationship} className="flex flex-col sm:flex-row gap-2" aria-label="Lägg till relation">
          <label htmlFor="rel-target" className="sr-only">
            Målkälla
          </label>
          <select
            id="rel-target"
            value={relTarget}
            onChange={(e) => setRelTarget(e.target.value)}
            className="flex-1 rounded-lg border border-border bg-base px-3 py-2 text-sm"
          >
            <option value="">Välj källa…</option>
            {otherSources.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title}
              </option>
            ))}
          </select>
          <label htmlFor="rel-type" className="sr-only">
            Relationstyp
          </label>
          <select
            id="rel-type"
            value={relType}
            onChange={(e) => setRelType(e.target.value)}
            className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
          >
            {RELATIONSHIP_TYPES.map((t) => (
              <option key={t} value={t}>
                {RELATIONSHIP_LABELS[t]}
              </option>
            ))}
          </select>
          <button type="submit" disabled={!relTarget || addingRelationship} className="rounded-lg bg-accent px-4 py-2 text-sm disabled:opacity-50">
            Lägg till
          </button>
        </form>
        {relError && (
          <div role="alert" className="mt-2 text-sm text-red-300">
            {relError}
          </div>
        )}
      </section>

      <section aria-labelledby="delete-heading" className="rounded-xl border border-red-500/30 p-4">
        <h2 id="delete-heading" className="text-sm font-medium text-red-300 mb-2">
          Radera källa
        </h2>
        <p className="text-sm text-white/50 mb-3">
          Tar bort källan och gör dess innehåll osökbart omedelbart. Det går inte att ångra.
        </p>
        {!confirmingDelete ? (
          <button
            type="button"
            onClick={() => setConfirmingDelete(true)}
            className="rounded-lg border border-red-500/50 px-4 py-2 text-sm text-red-300"
          >
            Radera källa
          </button>
        ) : (
          <div className="flex gap-2">
            <button
              type="button"
              onClick={handleDelete}
              disabled={deleting}
              className="rounded-lg bg-red-500/80 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
            >
              {deleting ? "Raderar…" : "Bekräfta radering"}
            </button>
            <button
              type="button"
              onClick={() => setConfirmingDelete(false)}
              className="rounded-lg border border-border px-4 py-2 text-sm"
            >
              Avbryt
            </button>
          </div>
        )}
      </section>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-3">
      <div className="text-[10px] uppercase tracking-wide text-white/40">{label}</div>
      <div className="mt-1 text-sm">{value}</div>
    </div>
  );
}

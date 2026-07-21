"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, ChatSource, Confidence, ConversationItem } from "@/lib/api";
import { useVoice } from "@/lib/useVoice";
import Orb, { OrbState } from "@/components/Orb";
import ConfidenceBadge from "@/components/ConfidenceBadge";

type ChatEntry = {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
  provider?: string;
  confidence?: Confidence;
  confidenceScore?: number;
  providersAttempted?: string[];
  conflictsDetected?: boolean;
};

export default function ChatPage() {
  const [conversations, setConversations] = useState<ConversationItem[]>([]);
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showErrorPulse, setShowErrorPulse] = useState(false);
  const [readAloud, setReadAloud] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);

  const voice = useVoice();
  const scrollRef = useRef<HTMLDivElement>(null);

  async function refreshConversations() {
    try {
      setConversations(await api.listConversations());
    } catch {
      // Non-critical for the chat flow itself — the list panel just stays empty/stale.
    }
  }

  useEffect(() => {
    // See justification in app/(shell)/documents/page.tsx — documented fetch-on-mount
    // pattern, setState happens after an await inside refreshConversations().
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refreshConversations();
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [entries, loading]);

  async function openConversation(id: string) {
    setHistoryOpen(false);
    try {
      const detail = await api.getConversation(id);
      setConversationId(detail.id);
      setEntries(
        detail.messages
          .filter((m) => m.role !== "system")
          .map((m) => ({ role: m.role as "user" | "assistant", content: m.content, provider: m.provider ?? undefined }))
      );
    } catch (e: any) {
      setError(e.message);
    }
  }

  function startNewConversation() {
    setConversationId(undefined);
    setEntries([]);
    setHistoryOpen(false);
  }

  async function deleteConversation(id: string, e: React.MouseEvent) {
    e.stopPropagation();
    await api.deleteConversation(id);
    if (id === conversationId) startNewConversation();
    await refreshConversations();
  }

  function flashError() {
    setShowErrorPulse(true);
    setTimeout(() => setShowErrorPulse(false), 1600);
  }

  async function send(message: string) {
    if (!message.trim() || loading) return;
    setEntries((prev) => [...prev, { role: "user", content: message }]);
    setInput("");
    setLoading(true);
    setError(null);
    try {
      const res = await api.sendChat(message, conversationId);
      setConversationId(res.conversation_id);
      setEntries((prev) => [
        ...prev,
        {
          role: "assistant",
          content: res.reply,
          sources: res.sources,
          provider: `${res.provider}/${res.model}`,
          confidence: res.confidence,
          confidenceScore: res.confidence_score,
          providersAttempted: res.providers_attempted,
          conflictsDetected: res.conflicts_detected,
        },
      ]);
      if (readAloud) voice.speak(res.reply);
      refreshConversations();
    } catch (e: any) {
      setError(e.message);
      flashError();
    } finally {
      setLoading(false);
    }
  }

  function handleMic() {
    if (voice.listening) {
      voice.stopListening();
      return;
    }
    voice.startListening(
      (text) => send(text),
      () => flashError()
    );
  }

  const orbState: OrbState = showErrorPulse
    ? "error"
    : voice.listening
      ? "listening"
      : loading
        ? "thinking"
        : voice.speaking
          ? "speaking"
          : "idle";

  return (
    <div className="flex h-[calc(100vh-2rem)] md:h-[calc(100vh-4rem)] gap-4">
      {/* Conversation history — drawer on mobile, static column on desktop */}
      <div
        className={`${historyOpen ? "flex" : "hidden"} md:flex fixed md:static inset-0 z-20 md:z-auto md:w-64 shrink-0 flex-col bg-base/95 md:bg-transparent p-4 md:p-0`}
      >
        <div className="flex items-center justify-between mb-3 md:mb-2">
          <h2 className="text-sm font-medium text-white/70">Konversationer</h2>
          <button
            type="button"
            onClick={() => setHistoryOpen(false)}
            className="md:hidden text-xs text-white/50"
            aria-label="Stäng historik"
          >
            Stäng
          </button>
        </div>
        <button
          type="button"
          onClick={startNewConversation}
          className="mb-3 rounded-lg border border-border px-3 py-2 text-left text-sm hover:bg-white/5"
        >
          + Ny konversation
        </button>
        <div className="flex-1 overflow-y-auto space-y-1" aria-label="Tidigare konversationer">
          {conversations.map((c) => (
            <div
              key={c.id}
              role="button"
              tabIndex={0}
              onClick={() => openConversation(c.id)}
              onKeyDown={(e) => e.key === "Enter" && openConversation(c.id)}
              className={`group flex items-center justify-between rounded-lg px-3 py-2 text-sm cursor-pointer ${
                c.id === conversationId ? "bg-accent/20 text-white" : "text-white/60 hover:bg-white/5"
              }`}
            >
              <span className="truncate">{c.title}</span>
              <button
                type="button"
                onClick={(e) => deleteConversation(c.id, e)}
                aria-label={`Ta bort konversationen ${c.title}`}
                className="ml-2 shrink-0 text-white/30 opacity-0 group-hover:opacity-100 hover:text-red-300"
              >
                ✕
              </button>
            </div>
          ))}
          {conversations.length === 0 && <div className="text-xs text-white/30 px-3">Inga konversationer ännu.</div>}
        </div>
      </div>

      {/* Main chat column */}
      <div className="flex flex-1 min-w-0 flex-col">
        <div className="flex items-center justify-between mb-2">
          <button
            type="button"
            onClick={() => setHistoryOpen(true)}
            className="md:hidden rounded-lg border border-border px-3 py-1.5 text-sm"
          >
            Historik
          </button>
          <h1 className="text-xl font-semibold hidden md:block">Chat med MainAI</h1>
          {voice.synthesisSupported && (
            <label className="flex items-center gap-2 text-xs text-white/50">
              <input
                type="checkbox"
                checked={readAloud}
                onChange={(e) => setReadAloud(e.target.checked)}
                aria-label="Läs upp svar automatiskt"
              />
              Läs upp svar
            </label>
          )}
        </div>

        <div className="flex justify-center py-2">
          <Orb state={orbState} size={110} />
        </div>

        <div ref={scrollRef} className="flex-1 overflow-y-auto space-y-4 pr-2" aria-live="polite">
          {entries.length === 0 && (
            <div className="text-white/40 text-sm text-center">
              Ställ en fråga om företaget, ett dokument eller ett projekt. Svaret bygger på det
              som är indexerat i kunskapsbiblioteket.
            </div>
          )}
          {entries.map((entry, i) => (
            <div key={i} className={`max-w-2xl ${entry.role === "user" ? "ml-auto" : ""}`}>
              <div
                className={`rounded-xl px-4 py-3 text-sm whitespace-pre-wrap ${
                  entry.role === "user" ? "bg-accent/20 border border-accent/30" : "bg-panel border border-border"
                }`}
              >
                {entry.content}
              </div>
              {entry.confidence && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <ConfidenceBadge confidence={entry.confidence} score={entry.confidenceScore ?? 0} />
                  {/* Trust Engine's conflicts_detected (app/rag/trust.py) was computed and
                      returned by every /api/chat response but never shown anywhere in this
                      UI — a founder had no visible warning that a source-level or
                      claim-level contradiction (app/rag/trust.py's detect_conflicts /
                      detect_claim_conflicts) was flagged internally, even next to a "hög
                      tillförlitlighet" badge. Found during STEG 14's full vertical review. */}
                  {entry.conflictsDetected && (
                    <span
                      role="alert"
                      className="inline-flex items-center gap-1.5 rounded-full border border-red-500/30 bg-red-500/15 px-2.5 py-1 text-xs text-red-300"
                      title="Källorna bakom svaret motsäger varandra — se källorna nedan innan du litar på svaret."
                    >
                      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
                      Motstridiga källor
                    </span>
                  )}
                </div>
              )}
              {entry.sources && entry.sources.length > 0 && (
                <div className="mt-2 text-xs text-white/40 space-y-1">
                  <div>Källor:</div>
                  {entry.sources.map((s, j) => (
                    <Link
                      key={j}
                      // STEG 12/13: a citation from a timed transcript chunk carries
                      // start_seconds — the ?t= param is read by the library detail page
                      // (app/(shell)/library/[id]/page.tsx) to seek+play the media element
                      // to that exact moment, not just open the source.
                      href={
                        s.start_seconds != null
                          ? `/library/${s.document_id}?t=${s.start_seconds}`
                          : `/library/${s.document_id}`
                      }
                      className="block pl-2 border-l border-border hover:border-accent hover:text-white/70"
                      title={
                        s.start_seconds != null
                          ? "Öppna källan i Founder Knowledge Studio och spela upp från citatet"
                          : "Öppna källan i Founder Knowledge Studio"
                      }
                    >
                      {s.title} ({s.score.toFixed(2)})
                      {s.active_truth_status && s.active_truth_status !== "active" && (
                        <span className="ml-1 text-amber-300/70">[{s.active_truth_status}]</span>
                      )}
                      {s.start_seconds != null && (
                        <span className="ml-1 text-white/30">▶ {Math.floor(s.start_seconds / 60)}:{(Math.floor(s.start_seconds) % 60).toString().padStart(2, "0")}</span>
                      )}
                    </Link>
                  ))}
                </div>
              )}
              {entry.providersAttempted && entry.providersAttempted.length > 1 && (
                <div className="mt-1 text-[11px] text-amber-300/70">
                  Bytte leverantör automatiskt: {entry.providersAttempted.join(" → ")}
                </div>
              )}
              {entry.provider && <div className="mt-1 text-[11px] text-white/25">{entry.provider}</div>}
            </div>
          ))}
          {loading && (
            <div className="text-white/40 text-sm" role="status">
              MainAI skriver…
            </div>
          )}
          {error && (
            <div role="alert" className="text-red-300 text-sm">
              Fel: {error}
            </div>
          )}
        </div>

        <div className="mt-4 flex gap-2">
          {voice.recognitionSupported && (
            <button
              type="button"
              onClick={handleMic}
              aria-pressed={voice.listening}
              aria-label={voice.listening ? "Sluta lyssna" : "Prata med MainAI"}
              className={`rounded-lg border px-4 py-3 text-sm ${
                voice.listening ? "border-emerald-400 text-emerald-300" : "border-border text-white/60"
              }`}
            >
              🎙
            </button>
          )}
          <label htmlFor="chat-input" className="sr-only">
            Meddelande till MainAI
          </label>
          <input
            id="chat-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send(input)}
            placeholder="Fråga MainAI vad som helst…"
            className="flex-1 rounded-lg border border-border bg-panel px-4 py-3 text-sm outline-none focus:border-accent"
          />
          <button
            type="button"
            onClick={() => send(input)}
            disabled={loading}
            className="rounded-lg bg-accent px-5 py-3 text-sm font-medium disabled:opacity-50"
          >
            Skicka
          </button>
        </div>
      </div>
    </div>
  );
}

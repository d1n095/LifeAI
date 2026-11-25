"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  api,
  ChatResponse,
  ChatSource,
  Confidence,
  ContextStatus,
  ConversationItem,
  newClientMessageId,
} from "@/lib/api";
import { useVoice } from "@/lib/useVoice";
import Orb, { OrbState } from "@/components/Orb";
import ConfidenceBadge from "@/components/ConfidenceBadge";

type FailedSend = {
  message: string;
  clientMessageId: string;
  conversationId?: string;
};

type ChatEntry = {
  role: "user" | "assistant";
  content: string;
  // Set on a user entry once the backend confirms it's saved (for a retry to target), and on
  // the corresponding assistant entry (to find/replace it on retry) — see
  // backend/app/routers/chat.py: the user message and the assistant reply are two
  // independently durable things, never lost together.
  userMessageId?: string;
  failed?: boolean;
  errorMessage?: string;
  retryable?: boolean;
  sources?: ChatSource[];
  provider?: string;
  confidence?: Confidence;
  confidenceScore?: number;
  providersAttempted?: string[];
  conflictsDetected?: boolean;
  // Set whenever the backend's retrieval found zero hits — see
  // backend/app/rag/context_status.py. Shown next to the reply so the founder can see the
  // VERIFIED reason (worker down, files still processing, no provider, genuinely no match...)
  // without depending on the model having said it correctly in its own reply text.
  contextStatus?: ContextStatus | null;
  groundingStatus?: "grounded" | "degraded" | "empty" | "unknown";
  groundingReason?: string | null;
};

function buildAssistantEntry(res: ChatResponse): ChatEntry {
  if (res.assistant_status === "failed") {
    return {
      role: "assistant",
      content: "",
      failed: true,
      errorMessage: res.error_message ?? "AI-svaret kunde inte genereras.",
      retryable: res.retryable,
      userMessageId: res.user_message_id,
      contextStatus: res.context_status,
      groundingStatus: res.grounding_status,
      groundingReason: res.grounding_reason,
    };
  }
  return {
    role: "assistant",
    content: res.reply ?? "",
    userMessageId: res.user_message_id,
    sources: res.sources,
    provider: `${res.provider}/${res.model}`,
    confidence: res.confidence ?? undefined,
    confidenceScore: res.confidence_score ?? undefined,
    providersAttempted: res.providers_attempted,
    conflictsDetected: res.conflicts_detected,
    contextStatus: res.context_status,
    groundingStatus: res.grounding_status,
    groundingReason: res.grounding_reason,
  };
}

// Short, founder-facing label for the badge — the full verified sentence (entry.contextStatus
// .message) is already shown below it, this is just what a founder scans for at a glance.
const CONTEXT_STATUS_LABELS: Record<ContextStatus["reason"], string> = {
  worker_unavailable: "Worker otillgänglig",
  files_processing: "Filer bearbetas",
  awaiting_provider: "Väntar på leverantör",
  search_provider_unavailable: "Sökning otillgänglig",
  indexing_failed: "Indexering misslyckades",
  no_relevant_match: "Ingen relevant träff",
  no_documents: "Inga filer uppladdade",
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
  // A request-level failure (network drop, proxy timeout) is different from an assistant
  // failure: the server may already have durably saved the message and even generated the
  // reply. Keep the exact idempotency key so "Försök igen" resumes the same logical send
  // instead of creating a second message/provider call.
  const [failedSend, setFailedSend] = useState<FailedSend | null>(null);
  const [streamingText, setStreamingText] = useState("");
  // The backend chooses the active, authorized chat model; this is a read-only display of
  // that real selection, not a second model picker or a client-side guess.
  const [modelState, setModelState] = useState<"loading" | "ready" | "missing" | "error">("loading");
  const [modelLabel, setModelLabel] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

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
    // The active thread is addressable as /chat?conversation=<id>, so a browser refresh
    // (or a shared/back-forward navigation) restores the durable transcript instead of
    // silently starting a new empty conversation. The list is loaded in parallel so the
    // history panel still works when the URL names a thread.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refreshConversations();
    const requested = new URLSearchParams(window.location.search).get("conversation");
    if (requested) {
      openConversation(requested);
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let cancelled = false;
    api
      .providerStatus()
      .then((providers) => {
        if (cancelled) return;
        const active = providers.find((provider) => provider.active_chat);
        if (!active) {
          setModelState("missing");
          setModelLabel(null);
          return;
        }
        // The live selection is the exact configured model. The latest verification may
        // belong to a different tag, so it is only a fallback for older backends.
        const model = active.active_chat_model ?? active.chat_verification?.model;
        setModelLabel(`${active.name}${model ? ` · ${model}` : ""}`);
        setModelState("ready");
      })
      .catch(() => {
        if (!cancelled) setModelState("error");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [entries, loading, streamingText]);

  useEffect(() => () => abortRef.current?.abort(), []);

  function rememberConversation(id: string | undefined) {
    const next = id ? `/chat?conversation=${encodeURIComponent(id)}` : "/chat";
    window.history.replaceState(null, "", next);
  }

  async function openConversation(id: string) {
    setHistoryOpen(false);
    try {
      const detail = await api.getConversation(id);
      setConversationId(detail.id);
      rememberConversation(detail.id);
      const restored: ChatEntry[] = [];
      for (const m of detail.messages) {
        if (m.role === "system") continue;
        if (m.role === "assistant" && m.status === "failed") {
          // The live response already showed this failure; a reload must not silently drop
          // it just because the stored content is empty. `in_reply_to_id` is the durable
          // user-message target the existing retry endpoint expects.
          restored.push({
            role: "assistant",
            content: "",
            failed: true,
            errorMessage: m.error_category
              ? `AI-svaret kunde inte genereras (${m.error_category}).`
              : "AI-svaret kunde inte genereras.",
            retryable: m.error_category === "unreachable" || m.error_category === "rate_limited",
            userMessageId: m.in_reply_to_id ?? undefined,
          });
          continue;
        }
        restored.push({
          role: m.role as "user" | "assistant",
          content: m.content,
          provider: m.provider ?? undefined,
          userMessageId: m.role === "user" ? m.id : m.in_reply_to_id ?? undefined,
        });
      }
      // A crash can leave the durable user message without any assistant row at all.
      // The live request never returned, so the normal failed-assistant entry above does
      // not exist. Surface the recoverable failure on the trailing user message so the
      // founder can retry the exact saved message instead of retyping it (which would
      // create a duplicate).
      const last = detail.messages[detail.messages.length - 1];
      const hasReply = detail.messages.some(
        (m) => m.role === "assistant" && m.in_reply_to_id === last?.id
      );
      if (last?.role === "user" && !hasReply) {
        restored.push({
          role: "assistant",
          content: "",
          failed: true,
          errorMessage: "AI-svaret saknas efter ett avbrott. Ditt meddelande finns sparat.",
          retryable: true,
          userMessageId: last.id,
        });
      }
      setEntries(restored);
      setFailedSend(null);
    } catch (e: any) {
      setError(e.message);
    }
  }

  function startNewConversation() {
    setConversationId(undefined);
    setEntries([]);
    setFailedSend(null);
    setHistoryOpen(false);
    rememberConversation(undefined);
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

  function applyChatResponse(res: ChatResponse, message: string) {
    setConversationId(res.conversation_id);
    rememberConversation(res.conversation_id);
    // The user's message is ALWAYS saved by this point (backend commits it before ever
    // calling a provider) — attach the real id to the optimistic bubble we already
    // rendered, then append the assistant's reply (or its failure) as its own entry. A
    // failed assistant_status never means the message above it was lost.
    setEntries((prev) => {
      const next = [...prev];
      for (let i = next.length - 1; i >= 0; i--) {
        if (next[i].role === "user" && next[i].content === message && !next[i].userMessageId) {
          next[i] = { ...next[i], userMessageId: res.user_message_id };
          break;
        }
      }
      next.push(buildAssistantEntry(res));
      return next;
    });
    if (res.assistant_status === "succeeded" && res.reply && readAloud) voice.speak(res.reply);
    if (res.assistant_status === "failed") flashError();
    refreshConversations();
  }

  async function runStream(
    message: string,
    targetConversationId: string | undefined,
    clientMessageId: string
  ) {
    setLoading(true);
    setError(null);
    setStreamingText("");
    const controller = new AbortController();
    abortRef.current = controller;
    let streamedUserMessageId: string | undefined;
    let streamedErrorMessage: string | undefined;
    let sawDone = false;
    try {
      const res = await api.streamChat(message, targetConversationId, clientMessageId, {
        signal: controller.signal,
        onEvent: (event) => {
          if (event.type === "start") streamedUserMessageId = event.user_message_id;
          else if (event.type === "delta") setStreamingText((prev) => prev + event.text);
          else if (event.type === "done") {
            sawDone = true;
            setStreamingText("");
          } else if (event.type === "error") {
            streamedErrorMessage = event.message;
          }
        },
      });
      applyChatResponse(res, message);
    } catch (e: any) {
      setStreamingText("");
      if (e?.name === "AbortError") {
        // The user explicitly cancelled. The server sees the disconnect and persists an
        // honest terminal state; keep the saved user message retryable instead of resending
        // it as a brand-new message.
        if (!sawDone && streamedUserMessageId) {
          setEntries((prev) => {
            if (prev.some((x) => x.role === "assistant" && x.userMessageId === streamedUserMessageId)) {
              return prev;
            }
            return [
              ...prev,
              {
                role: "assistant",
                content: "",
                failed: true,
                errorMessage: "Genereringen avbröts. Ditt meddelande finns sparat.",
                retryable: true,
                userMessageId: streamedUserMessageId,
              },
            ];
          });
        } else if (!sawDone) {
          setFailedSend({ message, clientMessageId, conversationId: targetConversationId });
        }
        setError("Genereringen avbröts.");
        return;
      }
      if (!sawDone && streamedErrorMessage && streamedUserMessageId) {
        setEntries((prev) => [
          ...prev,
          {
            role: "assistant",
            content: "",
            failed: true,
            errorMessage: streamedErrorMessage,
            retryable: false,
            userMessageId: streamedUserMessageId,
          },
        ]);
        setError(streamedErrorMessage);
        flashError();
        return;
      }
      // A request-level failure (network down, proxy timeout, session expired) is distinct
      // from assistant_status:"failed": the server may already have saved the message and
      // completed the reply. Preserve the key so retry resumes the SAME logical send.
      setFailedSend({ message, clientMessageId, conversationId: targetConversationId });
      setError(e.message);
      flashError();
    } finally {
      abortRef.current = null;
      setLoading(false);
    }
  }

  async function send(message: string) {
    if (!message.trim() || loading) return;
    const clientMessageId = newClientMessageId();
    setFailedSend(null);
    setEntries((prev) => [...prev, { role: "user", content: message }]);
    setInput("");
    await runStream(message, conversationId, clientMessageId);
  }

  async function retryFailedSend() {
    if (!failedSend || loading) return;
    const pending = failedSend;
    setFailedSend(null);
    await runStream(pending.message, pending.conversationId, pending.clientMessageId);
  }

  function cancelStream() {
    abortRef.current?.abort();
  }

  async function retry(userMessageId: string) {
    if (loading) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.retryChatMessage(userMessageId);
      setEntries((prev) => {
        const next = [...prev];
        const idx = next.findIndex((e) => e.role === "assistant" && e.userMessageId === userMessageId);
        const entry = buildAssistantEntry(res);
        if (idx >= 0) next[idx] = entry;
        else next.push(entry);
        return next;
      });
      if (res.assistant_status === "succeeded" && res.reply && readAloud) voice.speak(res.reply);
      if (res.assistant_status === "failed") flashError();
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

        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-white/40" role="status" aria-live="polite">
          {modelState === "loading" && <span>Kontrollerar aktiv modell…</span>}
          {modelState === "ready" && (
            <>
              <span>Aktiv modell:</span>
              <span className="text-white/65">{modelLabel}</span>
            </>
          )}
          {modelState === "missing" && (
            <span className="text-amber-300">Ingen aktiv chat-modell är konfigurerad.</span>
          )}
          {modelState === "error" && (
            <span className="text-amber-300">Modellstatus kunde inte läsas.</span>
          )}
          {(modelState === "missing" || modelState === "error") && (
            <Link href="/admin" className="text-accent2 hover:underline">
              Konfigurera modell
            </Link>
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
              {entry.failed ? (
                <div role="alert" className="rounded-xl px-4 py-3 text-sm bg-red-500/10 border border-red-500/30">
                  <div className="text-red-300">Ditt meddelande sparades, men AI-svaret kunde inte genereras.</div>
                  <div className="mt-1 text-white/50 text-xs">{entry.errorMessage}</div>
                  {entry.retryable ? (
                    <button
                      type="button"
                      onClick={() => entry.userMessageId && retry(entry.userMessageId)}
                      disabled={loading}
                      className="mt-2 rounded border border-red-500/30 px-3 py-1 text-xs text-red-300 hover:bg-red-500/10 disabled:opacity-30"
                    >
                      Försök igen
                    </button>
                  ) : (
                    <div className="mt-2 text-white/40 text-xs">
                      Det här felet kan inte lösas genom att försöka igen. Kontrollera leverantören
                      eller försök med en ny fråga.
                    </div>
                  )}
                </div>
              ) : (
                <div
                  className={`rounded-xl px-4 py-3 text-sm whitespace-pre-wrap ${
                    entry.role === "user" ? "bg-accent/20 border border-accent/30" : "bg-panel border border-border"
                  }`}
                >
                  {entry.content}
                </div>
              )}
              {entry.contextStatus && entry.groundingStatus !== "degraded" && (
                <div
                  className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200"
                  title="Verifierad orsak från biblioteket/workern — inte en gissning från AI-svaret."
                >
                  <span className="font-medium">{CONTEXT_STATUS_LABELS[entry.contextStatus.reason]}:</span>{" "}
                  {entry.contextStatus.message}
                </div>
              )}
              {entry.groundingStatus === "degraded" && (
                <div
                  role="status"
                  className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200"
                  title="Kunskapssökningen kunde inte köras. Svaret är därför uttryckligen utan kunskapsunderlag."
                >
                  <span className="font-medium">Svar utan kunskapsunderlag:</span>{" "}
                  {entry.groundingReason ?? "embedding-leverantören är inte tillgänglig för guardad chat."}
                </div>
              )}
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
            <div className="max-w-2xl" role="status" aria-live="polite">
              <div className="rounded-xl border border-border bg-panel px-4 py-3 text-sm whitespace-pre-wrap">
                {streamingText || "MainAI skriver…"}
              </div>
            </div>
          )}
          {error && (
            <div role="alert" className="text-red-300 text-sm">
              <div>Fel: {error}</div>
              {failedSend && (
                <button
                  type="button"
                  onClick={retryFailedSend}
                  disabled={loading}
                  className="mt-2 rounded border border-red-500/30 px-3 py-1 text-xs text-red-300 hover:bg-red-500/10 disabled:opacity-30"
                >
                  Försök igen
                </button>
              )}
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
          {loading ? (
            <button
              type="button"
              onClick={cancelStream}
              className="rounded-lg border border-red-500/40 px-5 py-3 text-sm font-medium text-red-300 hover:bg-red-500/10"
            >
              Avbryt
            </button>
          ) : (
            <button
              type="button"
              onClick={() => send(input)}
              className="rounded-lg bg-accent px-5 py-3 text-sm font-medium"
            >
              Skicka
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

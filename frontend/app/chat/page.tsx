"use client";

import { useState } from "react";
import { api, ChatSource } from "@/lib/api";

type ChatEntry = {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
  provider?: string;
};

export default function ChatPage() {
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [input, setInput] = useState("");
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send() {
    if (!input.trim() || loading) return;
    const message = input.trim();
    setEntries((prev) => [...prev, { role: "user", content: message }]);
    setInput("");
    setLoading(true);
    setError(null);
    try {
      const res = await api.sendChat(message, conversationId);
      setConversationId(res.conversation_id);
      setEntries((prev) => [
        ...prev,
        { role: "assistant", content: res.reply, sources: res.sources, provider: `${res.provider}/${res.model}` },
      ]);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex flex-col h-[calc(100vh-4rem)]">
      <h1 className="text-xl font-semibold mb-4">Chat med MainAI</h1>

      <div className="flex-1 overflow-y-auto space-y-4 pr-2">
        {entries.length === 0 && (
          <div className="text-white/40 text-sm">
            Ställ en fråga om företaget, ett dokument eller ett projekt. Svaret bygger på det som är
            indexerat i kunskapsbiblioteket.
          </div>
        )}
        {entries.map((entry, i) => (
          <div key={i} className={`max-w-2xl ${entry.role === "user" ? "ml-auto" : ""}`}>
            <div
              className={`rounded-xl px-4 py-3 text-sm ${
                entry.role === "user" ? "bg-accent/20 border border-accent/30" : "bg-panel border border-border"
              }`}
            >
              {entry.content}
            </div>
            {entry.sources && entry.sources.length > 0 && (
              <div className="mt-2 text-xs text-white/40 space-y-1">
                <div>Källor:</div>
                {entry.sources.map((s, j) => (
                  <div key={j} className="pl-2 border-l border-border">
                    {s.title} ({s.score.toFixed(2)})
                  </div>
                ))}
              </div>
            )}
            {entry.provider && <div className="mt-1 text-[11px] text-white/25">{entry.provider}</div>}
          </div>
        ))}
        {loading && <div className="text-white/40 text-sm">MainAI skriver…</div>}
        {error && <div className="text-red-300 text-sm">Fel: {error}</div>}
      </div>

      <div className="mt-4 flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Fråga MainAI vad som helst…"
          className="flex-1 rounded-lg border border-border bg-panel px-4 py-3 text-sm outline-none focus:border-accent"
        />
        <button
          onClick={send}
          disabled={loading}
          className="rounded-lg bg-accent px-5 py-3 text-sm font-medium disabled:opacity-50"
        >
          Skicka
        </button>
      </div>
    </div>
  );
}

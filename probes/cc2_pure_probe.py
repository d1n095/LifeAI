"""Examiner-only pure probes for e21ad76: outbound boundary, inbound gating, entity binding."""
import asyncio
from app.mainai_continuous_conversation.classify import classify_inbound
from app.mainai_continuous_conversation.discover import discover_software_truth
from app.mainai_continuous_conversation.service import compose_founder_reply
from app.mainai_runtime_contract import sanitize_unverified_execution_claims

def boundary(t): return compose_founder_reply(draft=sanitize_unverified_execution_claims(t), discovered=None)

FOUNDER_RELAY = ["Kan du klistra in SHA?", "Är CI grönt?", "Vad sa Cursor?", "Vad sa Claude?", "Could you share the commit id?", "What did Cursor report?", "Is CI green?"]
NATURAL_RELAY = ["Can you drop the latest commit hash in chat?", "Let me know whether the pipeline is green.", "Could you check if CI passed and tell me?",
    "Can you forward Codex's reply to me?", "Which branch is Codex working on?", "Send me the test output from Cursor.",
    "Kan du skicka testresultaten?", "Vilken branch jobbar Codex på?", "Kan du vidarebefordra det här till Claude?", "Vad rapporterade Codex?"]
LEGIT = ["A SHA is a 40-character hash that identifies a commit.", "You can configure GitHub Actions to run pytest on every push.",
    "Which branch of mathematics studies change? Calculus.", "Your CI status page lists every workflow run.",
    "Git stores each commit as a snapshot; a branch is just a movable pointer.", "Tests in pytest are discovered by the test_ prefix.",
    "GitHub hosts the repository; CI runs on each push."]
def show(label, items, want_changed):
    bad = [(m, boundary(m)) for m in items if (boundary(m) != m) != want_changed]
    print(f"{label}: {len(items)-len(bad)}/{len(items)} correct")
    for m, r in bad: print(f"    {'LEAKED' if want_changed else 'REPLACED'}: {m!r}" + ("" if want_changed else f"\n        -> {r[:110]!r}"))
show("MainAI drafts asking founder to relay (founder list) caught", FOUNDER_RELAY, True)
show("natural relay asks caught", NATURAL_RELAY, True)
show("legit replies untouched", LEGIT, False)

print("--- inbound founder-authority gating")
for t in ["Radera grenen codex/x", "Ska vi lansera nu?", "Ändra säkerhetspolicyn", "Godkänn köpet", "Ta bort databasen", "Betala fakturan på 4000 kr", "Delete branch codex/x", "Approve the purchase"]:
    c = classify_inbound(t); print(f"   {t!r:34} kind={c.kind.value:20} interrupt={c.interrupt_founder} ({c.interrupt.value})")

print("--- entity binding (GitHub client unconfigured)")
class NoGH:
    def is_configured(self): return False
Q = ["What is the frozen Founder Alpha SHA?", "What is the Continuous Conversation parent SHA?", "What is the current Continuous Conversation P1 fix SHA?",
     "Which commit did we freeze Founder Alpha on?", "What's the parent of the continuous conversation lane?", "Which SHA is the P1 fix for the conversation work at right now?",
     "Vilken SHA har Founder Alpha?", "Vad är föräldra-SHA:n för continuous conversation?", "Vad är SHA för P1-fixen just nu?", "What SHA is main at?"]
for q in Q:
    t = asyncio.run(discover_software_truth(q, client=NoGH()))
    print(f"   {q!r:62} -> entity={t.entity_key:32} sha={t.sha} source={t.source}")

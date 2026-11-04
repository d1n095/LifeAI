from __future__ import annotations

CONTEXT_CONTRACT = {
    "structured_context": ["CURRENT_APP", "CURRENT_FILE", "CURRENT_PROJECT", "CURRENT_REPO", "CURRENT_BRANCH", "CURRENT_SHA", "CURRENT_TASK"],
    "screen_vision_context": ["CURRENT_WINDOW", "VISIBLE_UI_CONTEXT", "CURRENT_SELECTION"],
    "network_context": ["CURRENT_URL"],
    "rule": "UNKNOWN != EMPTY; unavailable context must remain unknown",
}

LIFE_PLATFORM_REGISTRY = {
    "LIFE_OS_DESKTOP": "manual-first computer environment",
    "LIFE_BROWSER": "manual browser + optional MainAI assistance",
    "LIFE_MAP": "zoomable Life Graph / future / plan-vs-reality surface",
    "LIFE_STUDIO": "app builder for Life OS",
    "LIFE_APP_MARKET": "reviewed/signed third-party or AI-built app ecosystem",
    "LIFE_COMPUTER_CONTROLLER": "bounded OS/app/browser/file/terminal control",
    "LIFE_RESEARCH": "deep research/evidence system",
    "LIFE_LEARN": "learning/tutoring/skill-acquisition system",
    "LIFE_HOME": "smart home / IoT",
    "LIFE_DEV": "software/project building",
    "LIFE_FINANCE": "personal finance/investing learning and analysis",
}

LIFE_GRAPH_CONTRACT = {
    "pipeline": ["ingestion", "provenance", "classification", "deduplication", "contradiction", "supersession", "canonical_life_graph", "projections"],
    "classes": ["IDEA", "DREAM", "QUESTION", "PLAN", "DECISION", "REQUIREMENT", "TASK", "EVIDENCE", "IMPLEMENTATION", "VERIFIED_RESULT", "REJECTED", "SUPERSEDED", "UNKNOWN"],
    "invariants": ["FOUND != RELEVANT", "RELEVANT != CURRENT", "CURRENT != VERIFIED", "POTENTIAL != GOAL", "SUGGESTION != DECISION", "MENTIONED != IMPLEMENTED"],
}

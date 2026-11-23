"""Direct OpenAI consultation — MainAI/the founder consults an OpenAI model through the
official Responses API (https://api.openai.com/v1/responses), with no human forwarding step.

This module is deliberately STANDALONE from app/providers/' chat pipeline: the chat path
exists to answer users inside conversations, while this module is a tightly gated
consultation surface with its own, stricter contract:

* Fail-closed end to end (app/config.py's openai_consult_* settings, every default off/empty).
* Default-deny egress through app/egress_policy/service.py before anything leaves the process.
* Per-user daily token/cost budgets (app/openai_consult/budgets.py), reserved before the
  outbound call and settled with real usage after it.
* Rate-limited, founder-only HTTP surface (app/openai_consult/router.py).
* The only attachable tool is OpenAI's hosted read-only `web_search`, behind an explicit
  setting; client-supplied tools are structurally refused. No shell, filesystem, GitHub
  or code-execution tool exists in this module — there is nothing to misconfigure open.
* No database tables of its own (budgets live in Redis/in-memory counters) — the module
  cannot drift into migration conflicts with parallel work on other branches.
"""

from app.openai_consult.service import ConsultationConfigError, ConsultationServiceError, consult_openai
from app.openai_consult.schemas import ConsultIn, ConsultStatusOut

__all__ = [
    "ConsultIn",
    "ConsultStatusOut",
    "ConsultationConfigError",
    "ConsultationServiceError",
    "consult_openai",
]

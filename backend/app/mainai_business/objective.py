"""Generic declarative business-objective specification for the MainAI executive.

A `BusinessObjectiveSpec` describes *what* an executive should accomplish — the roles it
needs, the tasks and their dependencies — without hard-coding a single business. The engine
(`app.mainai_business.engine`) drives this spec through the REAL workforce substrate
(registry → selector → broker → task-scoped authority → independent verification →
performance ledger). The e-commerce reference below is one instance; the same shape serves
any industry or project type.

SAID != IMPLEMENTED: declaring a role/task here grants ZERO authority. Authority is minted
per task, scoped and time-bounded, by the broker — never by this spec.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleSpec:
    """A specialist seat in the team. Capability is what the selector scores against."""

    agent_key: str
    name: str
    role: str
    agent_type: str
    capability: str
    trust_zone: str = "LOCAL_INTERNAL"
    allowed_tool_classes: tuple[str, ...] = ("read_excerpt",)


@dataclass(frozen=True)
class TaskSpec:
    """One unit of delegated work. `depends_on` references other task_keys.

    `consequential=True` marks a task that produces an outward-facing / irreversible effect
    (publish, act, spend). The engine refuses to execute it without an explicit Founder
    approval token — fail-closed. It is never auto-run.
    """

    task_key: str
    title: str
    capability: str
    depends_on: tuple[str, ...] = ()
    risk: str = "low"
    data_sensitivity: str = "internal"
    consequential: bool = False
    allowed_read_paths: tuple[str, ...] = ("business/public/**",)
    allowed_tool_classes: tuple[str, ...] = ("read_excerpt",)


@dataclass(frozen=True)
class BusinessObjectiveSpec:
    objective_key: str
    title: str
    objective_text: str
    roles: tuple[RoleSpec, ...]
    tasks: tuple[TaskSpec, ...]
    verifier: RoleSpec

    def task_by_key(self, key: str) -> TaskSpec:
        for t in self.tasks:
            if t.task_key == key:
                return t
        raise KeyError(f"no task {key!r} in objective {self.objective_key!r}")

    def role_for_capability(self, capability: str) -> RoleSpec:
        for r in self.roles:
            if r.capability == capability:
                return r
        raise KeyError(f"no role provides capability {capability!r}")

    def validate(self) -> None:
        """Fail-closed structural checks: capabilities covered, deps resolvable, acyclic."""
        keys = {t.task_key for t in self.tasks}
        if len(keys) != len(self.tasks):
            raise ValueError("duplicate task_key in objective")
        capabilities = {r.capability for r in self.roles}
        for t in self.tasks:
            if t.capability not in capabilities:
                raise ValueError(f"task {t.task_key!r} needs capability {t.capability!r} with no role")
            for dep in t.depends_on:
                if dep not in keys:
                    raise ValueError(f"task {t.task_key!r} depends on unknown task {dep!r}")
                if dep == t.task_key:
                    raise ValueError(f"task {t.task_key!r} depends on itself")
        # Topological order also detects cycles.
        topological_order(self)

    def consequential_task_keys(self) -> tuple[str, ...]:
        return tuple(t.task_key for t in self.tasks if t.consequential)


def topological_order(spec: BusinessObjectiveSpec) -> list[TaskSpec]:
    """Dependency-respecting task order. Raises on a cycle (fail-closed)."""
    remaining = {t.task_key: set(t.depends_on) for t in spec.tasks}
    by_key = {t.task_key: t for t in spec.tasks}
    ordered: list[TaskSpec] = []
    while remaining:
        ready = sorted(k for k, deps in remaining.items() if not deps)
        if not ready:
            raise ValueError(f"dependency cycle among tasks: {sorted(remaining)}")
        for k in ready:
            ordered.append(by_key[k])
            del remaining[k]
            for deps in remaining.values():
                deps.discard(k)
    return ordered


# --------------------------------------------------------------------------------------
# Reference instance: synthetic e-commerce business opportunity assessment.
# Generic and reusable — swap the spec, keep the engine.
# --------------------------------------------------------------------------------------

_RESEARCH = RoleSpec(
    agent_key="biz-research",
    name="Market Research",
    role="lead_research",
    agent_type="RESEARCH",
    capability="market_research",
)
_FINANCE = RoleSpec(
    agent_key="biz-finance",
    name="Finance / Unit Economics",
    role="lead_finance",
    agent_type="FINANCE",
    capability="unit_economics",
)
_OPERATIONS = RoleSpec(
    agent_key="biz-operations",
    name="Operations / Fulfilment",
    role="lead_operations",
    agent_type="OPERATIONS",
    capability="fulfillment_ops",
)
_ANALYST = RoleSpec(
    agent_key="biz-analyst",
    name="Opportunity Analyst",
    role="lead_analysis",
    agent_type="ANALYST",
    capability="opportunity_synthesis",
)
_PUBLISHER = RoleSpec(
    agent_key="biz-publisher",
    name="Assessment Publisher",
    role="lead_publication",
    agent_type="PUBLISHER",
    capability="assessment_publication",
)
_VERIFIER = RoleSpec(
    agent_key="biz-verifier",
    name="Independent Verifier",
    role="verifier",
    agent_type="VERIFIER",
    capability="verification",
)

ECOMMERCE_OPPORTUNITY_ASSESSMENT = BusinessObjectiveSpec(
    objective_key="ecommerce_opportunity_assessment_v1",
    title="Synthetic e-commerce opportunity assessment",
    objective_text=(
        "Assess the opportunity for a synthetic direct-to-consumer e-commerce store selling "
        "a mid-price household product. Coordinate research, finance and operations to produce "
        "a go / conditional-go / no-go recommendation with evidence. No real purchases, "
        "payments, publications or external operations."
    ),
    roles=(_RESEARCH, _FINANCE, _OPERATIONS, _ANALYST, _PUBLISHER),
    verifier=_VERIFIER,
    tasks=(
        TaskSpec(
            task_key="market_research",
            title="Market & demand research",
            capability="market_research",
        ),
        TaskSpec(
            task_key="unit_economics",
            title="Unit economics & finance model",
            capability="unit_economics",
        ),
        TaskSpec(
            task_key="fulfillment_ops",
            title="Fulfilment & operations feasibility",
            capability="fulfillment_ops",
        ),
        TaskSpec(
            task_key="opportunity_synthesis",
            title="Opportunity synthesis & recommendation",
            capability="opportunity_synthesis",
            depends_on=("market_research", "unit_economics", "fulfillment_ops"),
        ),
        TaskSpec(
            task_key="publish_assessment",
            title="Publish opportunity assessment to the Founder",
            capability="assessment_publication",
            depends_on=("opportunity_synthesis",),
            consequential=True,
        ),
    ),
)

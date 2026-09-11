# MainAI V2 — Offline Knowledge Packs (Stage V2-F, Part C)

**Status:** design-only, isolated lane. Does not modify, rebase, or depend on PR #245 / exact
SHA `818dfb732da47901eb5ae06ffdd9c829fe00c4c5`. See `MAINAI_V2_ARCHITECTURE_MAP.md` (V2-A) for
shared vocabulary and the canonical security constitution.

---

## 0. What already exists — reuse, don't reinvent

- **`app.mainai_school.evidence`** already has a real evidence-hierarchy (`EvidenceRank`:
  DETERMINISTIC_TEST > PRIMARY_SOURCE > DIRECTLY_OBSERVED_OUTCOME > AUTHORITATIVE_DOMAIN_SOURCE
  > MULTIPLE_INDEPENDENT_EXPERTS > HISTORICAL_EVIDENCE > MODEL_OPINION) with the invariant
  `MODEL_CONSENSUS_IS_NOT_TRUTH`. A knowledge pack claim's supporting evidence should be typed
  using this SAME enum, not a new confidence scale — a pack claim backed only by
  `MODEL_OPINION`-rank evidence is fundamentally weaker than one backed by
  `PRIMARY_SOURCE` (the actual statute text), and MainAI's "I can answer with confidence Y"
  framing (§3) should say so.
- **`app.mainai_school.curriculum.run_independent_exam()`** exists and is the closest
  existing mechanism to a "pack validator/exam suite" — but this session found and (via PR
  #245's evidence-semantics fix) partially addressed a real bug where it took
  `local_passed: bool`/`score: float` as raw caller-supplied parameters with **no real
  grading or evidence row at all**. **Position taken here**: Offline Knowledge Packs should
  NOT reuse `run_independent_exam()` as-is. It should reuse the surrounding
  `CompetenceStatus` state machine (§1) and the general "exam mode: teacher must not help,
  one pass != permanent competence" doctrine already correctly stated in that module's
  docstring, but the exam-GRADING mechanism itself needs to be genuinely real for knowledge
  packs specifically: a pack's own `validators` (§1's schema field) must be able to
  DETERMINISTICALLY check an answer against the pack's own `claims`/`examples`, not accept a
  caller's self-report the way the current (already-flagged-broken) mechanism does. This is a
  concrete, real requirement for V2-F's implementation phase, not a nice-to-have.
- **`app.mainai_school.types.CompetenceStatus`** already has `UNTRAINED`, `LEARNING`,
  `SUPERVISED`, `PROBATION`, `LOCALLY_COMPETENT`, `LOCALLY_VERIFIED`, `DEGRADED`,
  `RETRAINING` — a near-exact match for the founder's V2-E competence vocabulary
  (`UNTRAINED`/`LEARNING`/`ASSISTED`/`PROVEN_LOCAL`/`EXPERT_LOCAL`/`STALE`). Knowledge packs
  track PACK-level currency (§2) as a distinct concept from AGENT-level competence
  (`MAINAI_V2_LOCAL_WORKFORCE.md`, V2-E) — a specialist's competence in a domain is informed
  by, but not identical to, the freshness of the knowledge pack(s) it draws on. A specialist
  can be `LOCALLY_VERIFIED` while its underlying pack is simultaneously flagged `stale` by
  §2's versioning rules — these are two different clocks and must not be conflated into one
  status field.
- **`app.evidence_claim.evidence_supports_claim()`**'s exact-subject-match discipline (fixed
  this session after finding it was previously satisfied by mere substring containment) is
  the direct model for §1's claim-to-source linkage requirement below.

---

## 1. Knowledge pack schema

```python
@dataclass(frozen=True)
class KnowledgePackManifest:
    pack_id: str                    # e.g. "se.consumer_law"
    display_name: str               # "Swedish Consumer Law"
    jurisdiction: str                # "SE" -- ISO-code, not free text, so packs can be filtered by applicability
    version: str                    # semver
    valid_from: date
    valid_until: date | None        # None = no known expiry, but see §2 -- absence of an
                                     #   expiry is NOT the same as "never needs re-checking"
    last_checked: date              # when a human/verified process last confirmed sources still current
    sources: tuple["PackSource", ...]
    claims: tuple["PackClaim", ...]
    workflows: tuple["PackWorkflow", ...]
    validators: tuple["PackValidator", ...]
    examples: tuple["PackExample", ...]
    edge_cases: tuple["PackEdgeCase", ...]
    known_failure_modes: tuple[str, ...]
    exam_suite_id: str               # references the real grading mechanism, see §0's position
    update_manifest: "PackUpdateManifest"


@dataclass(frozen=True)
class PackSource:
    source_id: str
    title: str
    url_or_citation: str
    source_kind: str                # maps onto EvidenceRank names, e.g. "PRIMARY_SOURCE" for
                                     #   actual statute text, "AUTHORITATIVE_DOMAIN_SOURCE" for
                                     #   a government agency's own guidance page
    retrieved_at: date
    checksum: str | None            # for machine-fetched sources, so silent upstream edits are detectable


@dataclass(frozen=True)
class PackClaim:
    claim_id: str
    statement: str                  # the actual claim, in plain language
    # EVIDENCE EXISTS != EVIDENCE SUPPORTS CLAIM, applied to knowledge packs: a claim is
    # useless without a traceable link to the SPECIFIC source paragraph/section that supports
    # it, not just "the pack has sources somewhere" -- same exact-subject-match discipline
    # evidence_claim.py now enforces for capability evidence.
    supporting_source_ids: tuple[str, ...]   # must be non-empty; each id must exist in sources
    evidence_rank: str               # strongest EvidenceRank among supporting_source_ids -- computed, not asserted
    superseded_by_claim_id: str | None = None   # supersession chain, never silent overwrite -- see §2


@dataclass(frozen=True)
class PackWorkflow:
    workflow_id: str
    trigger_description: str         # "owner asks about disputing a debt collection notice"
    steps: tuple[str, ...]
    referenced_claim_ids: tuple[str, ...]   # every step's factual basis must trace to a real claim


@dataclass(frozen=True)
class PackValidator:
    validator_id: str
    checks_claim_id: str
    validation_kind: str             # "deterministic_rule" | "worked_example_match" | "citation_lookup"
    # A REAL, machine-checkable assertion -- not a free-text description of what should be
    # tested. E.g. for a tax-bracket claim: {"input": {"income": 500000}, "expected_output":
    # {"bracket": "X", "rate": 0.32}}. This is the thing that makes exam grading real instead
    # of a caller's self-report (§0's position).
    check_spec: dict


@dataclass(frozen=True)
class PackExample:
    example_id: str
    scenario: str
    expected_guidance_summary: str
    references_workflow_id: str | None


@dataclass(frozen=True)
class PackEdgeCase:
    description: str
    why_it_breaks_naive_application: str
    correct_handling: str


@dataclass(frozen=True)
class PackUpdateManifest:
    upstream_check_frequency: str    # "monthly" | "on_law_change_notification" | ...
    upstream_watch_sources: tuple[str, ...]   # what MainAI/central watches to know a re-check is due
    last_update_check: date
    pending_review: bool             # true if a watched source changed and re-verification hasn't completed yet
```

## 2. Versioning and update flow — supersession, not silent overwrite

When Swedish Consumer Law is amended, the pack does **not** silently replace old claims in
place. This reuses the exact discipline already established in this codebase for
`EngineeringLesson` and `FounderMemoryNote` (both append-only with explicit supersession
links, never destructive overwrite):

1. A new `PackClaim` is added with `claim_id` distinct from the old one.
2. The OLD claim gets `superseded_by_claim_id` set to the new claim's id. It is never deleted
   — a workflow, example, or historical answer that referenced the old claim_id remains
   reconstructable ("what did the pack say about this on date X" stays answerable, matching
   this session's own multi-restart continuity requirement: superseded stays history-only,
   never presented as current, but never lost either).
3. The pack's `version` bumps (semver: a claim correction is at minimum a minor version, a
   claim REMOVAL with no replacement — e.g. a repealed law — is a major version since it can
   change workflow applicability).
4. Any `PackWorkflow` whose `referenced_claim_ids` includes a now-superseded claim is flagged
   `pending_review` in the update manifest — a workflow built on stale law must not silently
   keep operating on outdated claims just because no one told it to re-check.
5. `valid_until` on the OLD claim's containing version is set retroactively once the
   supersession is confirmed, so "was this pack version ever wrong" is answerable precisely,
   not just "is it wrong now."

**What "current" means when the owner asks something**: always resolve to the claim with no
`superseded_by_claim_id` for that logical claim lineage, at the pack's current version — the
identical "current truth is current, superseded remains history" invariant this whole session
built and stress-tested for founder memory notes, applied here to legal/domain claims instead.

## 3. Worked example — the actual API response

Owner asks (offline, no network): *"Can my landlord raise my rent by 15% this year?"*

```python
# MainAI queries the SE.housing pack (assume version 3.2.0, last_checked 2026-06-01)
{
  "answer_summary": "Generally no -- Swedish rent increases for regulated tenancies go "
                     "through the negotiation/Hyresnämnden process, not a unilateral "
                     "landlord increase, and a 15% jump would very likely be contestable.",
  "knowledge_source": {
    "pack_id": "se.housing",
    "pack_version": "3.2.0",
    "last_verified_date": "2026-06-01",
    "offline": True
  },
  "confidence": {
    "evidence_rank": "PRIMARY_SOURCE",   # from EvidenceRank -- this claim traces to actual statute text
    "claim_ids": ["se.housing.rent_increase_process.v2"],
    "currency_note": "Pack last checked 2026-06-01; if this is more than "
                      "{update_manifest.upstream_check_frequency} ago, flag as needing refresh."
  },
  "caveat": "This is general guidance, not a substitute for Hyresnämnden or legal counsel "
            "for your specific lease.",
  "workflow_offered": "se.housing.dispute_rent_increase.v1"
}
```

This is the concrete shape of **OFFLINE != BLIND CONFIDENCE**: every element of the response
traces to a specific pack version, a specific claim, a specific evidence rank, and an explicit
currency statement — never a bare answer with no provenance. If `evidence_rank` for the best
available claim were only `MODEL_OPINION` (no real source pack coverage), the response
structure forces that to be stated too, not hidden behind confident prose.

## 4. Explicitly out of scope for this document

- The actual authoring/curation pipeline for producing a pack (who writes `PackClaim`s, what
  review gate a new pack goes through before distribution) — an operational/business process
  question for V2-J's implementation plan, not an architecture question.
- Download/distribution mechanics (CDN, P2P, bundled-with-app) — implementation detail.
- Multi-jurisdiction conflict resolution (a claim that's true in one Swedish municipality but
  not another) — real complexity worth its own follow-up design pass once the first pack
  (single jurisdiction) proves the schema, not solved speculatively here.

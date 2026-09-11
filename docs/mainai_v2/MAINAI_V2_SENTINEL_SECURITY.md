# MainAI V2 — Sentinel Security Platform (Stage V2-D, covers Parts E/F/G/I)

**Status:** design-only, isolated lane. Does not modify, rebase, or depend on PR #245 / exact
SHA `818dfb732da47901eb5ae06ffdd9c829fe00c4c5`. See `MAINAI_V2_ARCHITECTURE_MAP.md` (V2-A) for
shared vocabulary, trust boundaries, and the canonical security constitution referenced
throughout this document by name.

---

## 0. What already exists — reuse, don't reinvent

Checked before writing this document:

- **`app.workforce.injection`** (`FORBIDDEN_AUTHORITY_KEYS`, `INJECTION_NEEDLES`,
  `scrub_authority_mutations()`, `looks_like_prompt_injection()`) is a real, already-shipped
  keyword/needle-based prompt-injection and authority-mutation-stripping mechanism for
  workforce agent output. **Important caveat, carried over from tonight's own findings**:
  this session found the SAME class of bug (fixed word-list, no semantic understanding,
  bypassable by trivial rewording, e.g. "girls" matching a substring "rls") independently in
  three other keyword-matching modules tonight (`#214`'s `_touches_protected()`, `#218`'s
  `classify_ambiguity()`, and by extension anything built the same way). `INJECTION_NEEDLES`
  is a plain substring list (`"ignore previous instructions"`, `"you are now"`, etc.) —
  **it almost certainly has the same evasion weakness** (a rewording like "disregard prior
  guidance" would not match). This document does NOT re-audit it empirically (that's a Lane
  A red-team task against existing code, not a V2 design task), but Sentinel's Input Security
  Pipeline (§3 below) must NOT simply extend this list for new content types — it should
  route through the SAME architecture-level fix already proposed tonight for the other three
  occurrences (`docs/BRANCH_REGISTRY.md`'s "semantic evasion" section): a cheap keyword
  fast-path, falling through to a real semantic classifier only when ambiguous, flagged for
  founder sign-off since it's a new billed AI-call surface. Sentinel becomes the FOURTH
  consumer of that same fix, not a reason to build a fifth bespoke keyword list.
- **`app.mainai_school.evidence`** already has a real, ranked evidence-hierarchy system
  (`EvidenceRank`: DETERMINISTIC_TEST > PRIMARY_SOURCE > DIRECTLY_OBSERVED_OUTCOME >
  AUTHORITATIVE_DOMAIN_SOURCE > MULTIPLE_INDEPENDENT_EXPERTS > HISTORICAL_EVIDENCE >
  MODEL_OPINION, invariant `MODEL_CONSENSUS_IS_NOT_TRUTH`) and a `resolve_local_vs_teacher()`
  arbiter. Sentinel's own threat-classification confidence (§1) should use this SAME ranking
  concept for "is this actually malicious" evidence, not invent a parallel confidence scale.
- **`app.execution_envelopes`** (propose-never-writes, authorize-always-explicit) is the
  reused primitive for Defensive Autonomy's pre-authorization object (§2).
- **`app.workforce.kill_switch`** / `workforce_authority_epoch` (DB-backed, `FOR SHARE`/`FOR
  UPDATE` serialized grant-vs-stop) is the reused primitive for actual containment actions
  (§2's action list) — Sentinel triggers it, does not reimplement it.

---

## 1. Sentinel domains and the SPECIALIZATION != SURVEILLANCE boundary

Sentinel domains (founder's list, verbatim): file protection, malware scanning, behavior
monitoring, ransomware protection, exploit monitoring, network IDS/IPS, application control,
script control, USB/device monitoring, browser/link isolation, attachment quarantine,
model/plugin verification, integrity monitoring, credential protection, data-exfiltration
detection, canary/honeypot detection, security event correlation.

Each domain is a **department** in the existing Stage T sense (`app.workforce.department_evidence`)
— a security specialist agent, not MainAI herself, runs the actual detection. Its
`allowed_data_classes` (existing `WorkforceAssignment` authority field) is scoped to exactly
what that domain needs and nothing else:

| Specialist | Gets | Never gets |
|---|---|---|
| Malware Agent | file hash, process tree, syscalls, sandbox result | finance history, private chat, documents |
| Network IDS Agent | connection metadata (src/dst/port/protocol/bytes), DNS queries | packet payload content of non-flagged traffic, browsing history unrelated to the flagged connection |
| Credential Protection Agent | which credential class was accessed, by which process, when | the credential value itself |
| Exfiltration Detection Agent | data volume/destination/timing patterns | the actual data content being moved (classified by type/size, not read) |
| Attachment Quarantine Agent | file type, hash, static/dynamic analysis result | the sender's identity/relationship context beyond what's needed to render a verdict |

**Enforcement, not policy**: this table becomes literal `allowed_data_classes` values on each
specialist's registration (`app/workforce/registry.py`'s existing model), the same way Stage
T already enforces `allowed_read_paths`/`allowed_tool_classes` for any other agent. A security
specialist that tries to read outside its declared data class hits the same authority
boundary any other agent would — Sentinel gets no special exemption from
`SPECIALIZATION != BROADER ACCESS`.

## 2. Security Event Mesh

### 2.1 Normalized event schema

```python
@dataclass(frozen=True)
class SecurityEvent:
    event_id: UUID
    owner_id: UUID
    event_type: str  # from the closed vocabulary below
    occurred_at: datetime
    source: str  # "kernel_hook" | "network_monitor" | "file_watcher" | "model_verifier" | ...
    subject: dict  # e.g. {"process_id": ..., "file_path": ..., "device_id": ...} -- shape varies by event_type, always structured, never free text
    severity_hint: str  # "info" | "suspicious" | "high" | "critical" -- the EMITTING sensor's own initial read, not a final verdict
    raw_evidence_ref: str | None  # pointer to a durable, immutable record of what triggered this (log line, hash, pcap fragment id) -- NEVER the mesh event's own payload duplicating large raw data
    correlation_keys: dict  # {"process_id": ..., "device_id": ..., "network_dest": ...} -- explicit, typed fields the correlator matches on, not a free-text blob


SECURITY_EVENT_TYPES = frozenset({
    "process_started", "process_injected", "binary_changed", "model_changed",
    "file_opened_massively", "credential_read_attempt", "vault_access_attempt",
    "network_connection", "suspicious_dns", "usb_connected", "bluetooth_pair_attempt",
    "browser_exploit_signal", "ransomware_pattern", "unexpected_egress",
    "agent_privilege_attempt", "policy_change", "boot_integrity_failure",
})
```

`raw_evidence_ref` matters: this is the exact same "evidence exists != evidence supports
claim" discipline from `app.evidence_claim`, applied to security events — a
`SecurityEvent` is a **claim** that something happened; the referenced raw record is what
actually supports it. An incident correlator (or a human/MainAI investigator) must always be
able to walk from a `SecurityEvent` to the durable raw evidence it claims to summarize, never
trust the event row's own severity_hint as ground truth.

### 2.2 Correlation-into-incidents design

Correlation is NOT a generic "cluster similar events" ML step — it is a deterministic,
auditable rule-matching engine over `correlation_keys`, because a security incident decision
must be explainable to the owner ("why did you block that?" — V2-A §1's `VISIBLE_SURFACE`).

```python
@dataclass(frozen=True)
class CorrelationRule:
    rule_id: str
    name: str
    # An ordered sequence of event_type patterns that must co-occur, sharing at least one
    # correlation_key value, within `window`. Each step can require a MINIMUM severity_hint.
    required_sequence: tuple[tuple[str, str], ...]  # (event_type, min_severity)
    shared_key: str  # which correlation_keys field must match across all steps, e.g. "device_id"
    window: timedelta
    resulting_severity: str
    resulting_incident_kind: str


# The founder's own worked example, as a real rule:
USB_MASS_EXFIL_RULE = CorrelationRule(
    rule_id="usb_mass_exfil_v1",
    name="USB device + unknown process + mass file read + new egress + credential read",
    required_sequence=(
        ("usb_connected", "info"),
        ("process_started", "suspicious"),   # the unknown process spawned after USB connect
        ("file_opened_massively", "suspicious"),
        ("unexpected_egress", "high"),
        ("credential_read_attempt", "high"),
    ),
    shared_key="device_id",  # or process_id, depending on which step -- see 2.3
    window=timedelta(minutes=10),
    resulting_severity="critical",
    resulting_incident_kind="suspected_exfiltration_via_removable_media",
)
```

**Matching logic (concrete, not "correlate them")**: maintain a sliding window per
`(owner_id, shared_key value)`. As each new `SecurityEvent` arrives, check every
`CorrelationRule` whose `required_sequence` contains that event's `event_type`: has every
EARLIER step in the sequence already been seen for this same key value, within `window` of
each other? If the full sequence completes, emit a `SecurityIncident` referencing every
contributing `SecurityEvent` (never summarizing them away — provenance survives). A single
rule engine evaluating N rules against a bounded recent-event window per correlation key is
O(events × active rules), not a full graph search — deliberately simple and auditable over
clever, matching this session's own "boring, auditable, testable" bias throughout tonight's
campaign.

**Cross-key correlation** (the USB example actually needs this: `usb_connected`'s key is
`device_id`, but `process_started`'s natural key is `process_id`): the engine needs a
secondary join — "this process was the first new process observed within N seconds of this
device_id's connection" is itself a small, explicit correlation step, not implicit magic. Model
this as a two-stage rule: an initial keying rule that binds `process_id -> device_id` for the
observation window, then the main sequence rule keys on the bound `process_id` for the
remaining steps. Write this out as real code before implementation, not left as prose — it's
the trickiest part of the whole mesh and deserves its own short design doc at implementation
time (flagged in V2-J).

### 2.3 Incident object

```python
@dataclass(frozen=True)
class SecurityIncident:
    incident_id: UUID
    owner_id: UUID
    rule_id: str
    severity: str
    kind: str
    contributing_event_ids: tuple[UUID, ...]  # never summarized away
    state: str  # NORMAL | SUSPECTED | CONTAINMENT | EMERGENCY | RECOVERY, see §3
    opened_at: datetime
    actions_taken: tuple[str, ...]  # references to real kill_switch/authority actions, see §3.3
    resolved_at: datetime | None
    resolution: str | None  # "false_positive" | "contained" | "owner_confirmed_benign" | ...
```

## 3. Defensive Autonomy

### 3.1 State machine

```
NORMAL -> SUSPECTED -> CONTAINMENT -> EMERGENCY -> RECOVERY -> NORMAL
             ^                                         |
             └─────────────── (false positive) ────────┘
```

- **NORMAL**: no active incident.
- **SUSPECTED**: a `SecurityIncident` opened at severity below the pre-authorized action
  threshold — MainAI surfaces it to the owner, takes no containment action yet.
- **CONTAINMENT**: severity crosses the pre-authorized threshold (§3.2) — bounded, reversible
  defensive actions execute automatically.
- **EMERGENCY**: containment did not stop escalation (e.g. exfiltration continuing after
  network block) — the MOST severe pre-authorized actions available execute (switch to
  LOCAL_ONLY, lock Vault) — still never anything outside the pre-authorized scope.
- **RECOVERY**: incident contained, forensic snapshot taken, awaiting owner/Guardian review
  before returning to NORMAL — this transition is NEVER automatic (matches `RECOVERY !=
  BACKDOOR`: recovering FROM an incident state still requires the same real authorization as
  clearing a kill-switch stop, reusing `clear_kill_switch_for_recovery()`'s existing—if
  currently weak, see V2-A §0—ack-gated pattern).

### 3.2 Pre-authorization object — the core design requirement

**MainAI/Sentinel cannot self-grant defensive autonomy scope.** This reuses
`app.execution_envelopes`'s propose-never-authorizes doctrine exactly:

```python
@dataclass(frozen=True)
class DefensiveAuthorizationScope:
    scope_id: UUID
    owner_id: UUID
    granted_by: str  # "owner" | "guardian" -- never "mainai"
    granted_at: datetime
    # Bounded, enumerable -- not "MainAI may defend as needed"
    allowed_actions: frozenset[str]  # subset of DEFENSIVE_ACTIONS, see 3.3
    severity_threshold: str  # minimum incident severity that may trigger allowed_actions automatically
    max_actions_per_incident: int  # a runaway containment loop must not be unbounded
    expires_at: datetime | None  # a stale, forgotten grant is not forever-standing authority
    revoked_at: datetime | None
```

Exactly like `authorize_execution_scope()` requires the caller's own explicit assertion (never
copies a `propose_execution_scope()` proposal), a live incident's containment action checks
**this durable, owner-granted row** — never a value Sentinel computed for itself in the
moment. If no valid, unexpired `DefensiveAuthorizationScope` covers the required action at the
incident's severity, Sentinel does NOT act automatically — it escalates to SUSPECTED-only
(surface to owner, wait) even under `EMERGENCY`-shaped circumstances. **A missing
authorization is not treated as an emergency exception** — this is the single most important
line in this whole document: `TIME_TO_DAMAGE < TIME_TO_ASK_OWNER` justifies acting WITHOUT
waiting for a live response, it never justifies acting without a PRE-EXISTING grant.

### 3.3 Bounded action list

```python
DEFENSIVE_ACTIONS = frozenset({
    "block_network_for_process",
    "revoke_agent_lease",          # existing kill_switch mechanism, generalized beyond workforce
    "quarantine_file",
    "disable_provider_access",     # existing execution_envelopes-adjacent mechanism
    "lock_vault",
    "disable_sensitive_sessions",
    "isolate_browser",
    "freeze_process",
    "snapshot_forensic_state",     # always allowed, never destructive, not authority-gated the same way
    "switch_to_local_only",
    "switch_to_safe_mode",
})
```

**What's explicitly, permanently NOT in this set, no matter the pre-authorization** (the
`NO OWNER RESPONSE != PERMISSION FOR EVERYTHING` boundary, concretely enumerated rather than
left as a policy note):
- No deletion of the owner's own files/data as a "defensive" measure (quarantine moves/isolates,
  never destroys — destructive action requires the SAME recovery-grade authorization as
  Sovereign Recovery's reset levels, V2-H, never a defensive-autonomy grant).
- No outbound network action targeting anything OTHER than the owner's own device/process
  (this is the hack-back wall — `block_network_for_process` blocks the LOCAL process's
  ability to talk out, it never sends anything TO the remote attacker).
- No credential rotation/password changes without a live owner confirmation (a compromised
  credential still needs the OWNER to know it happened and confirm the new one — MainAI
  silently rotating it could lock the owner out during exactly the incident where they most
  need access).
- No irreversible identity/device revocation (that's V2-G/V2-O territory, requiring
  stronger-than-login authorization by design — defensive autonomy can FREEZE a device
  session, never permanently revoke identity trust unilaterally).

Every action in `DEFENSIVE_ACTIONS` is reversible or non-destructive by construction —
`snapshot_forensic_state` is the one action that's always allowed regardless of
authorization scope, because it's purely additive (never modifies anything) and its evidence
is exactly what Guardian/owner needs to review during RECOVERY.

## 4. Input Security Pipeline

```
INPUT (PDF/Office/ZIP/image/audio/video/email/SMS/MMS/QR/browser/USB/Bluetooth/model/plugin)
  │
  ▼
1. QUARANTINE          — content lands in an isolated store, zero execution/parsing yet
  │
  ▼
2. TYPE VERIFICATION    — actual file-format sniffing (magic bytes), never trust the extension
  │                        or claimed MIME type (`FILE CONTENT != INSTRUCTION AUTHORITY`
  │                        starts here: a .pdf that's actually a script is caught structurally)
  ▼
3. SAFE PARSER          — format-specific parser chosen from an ALLOWLIST of known-safe
  │                        parsers for the verified type; unknown/unsupported type -> DEFAULT
  │                        DENY (see §4.1), never "try the most permissive parser available"
  ▼
4. SANDBOX (if needed)  — active content (macros, scripts, executable payloads) only ever
  │                        runs, if at all, inside a disposable sandbox with the SAME
  │                        Sentinel data-class boundary as any other specialist (§1)
  ▼
5. MALWARE/BEHAVIOR INSPECTION — Sentinel's malware-scanning domain (§1), emits
  │                        SecurityEvents into the mesh (§2) like any other detector
  ▼
6. METADATA SANITATION  — strip EXIF/document-metadata/tracking pixels/embedded objects
  │                        before the content is ever shown to MainAI or the owner
  ▼
7. PROMPT-INJECTION CLASSIFICATION — reuses `app.workforce.injection`'s existing mechanism
  │                        (§0's caveat applies: keyword fast-path + eventual semantic
  │                        fallback, not a bigger keyword list), extended so its scrubbing
  │                        target is arbitrary parsed content, not just workforce agent
  │                        JSON payloads — the underlying invariant is identical either way:
  │                        content is DATA, never instruction authority
  ▼
8. BOUNDED CONTENT REPRESENTATION — the final, sanitized, structurally-typed representation
     MainAI actually receives (e.g. "PDF, 4 pages, text extracted, no active content, no
     injection markers, metadata stripped") — never the raw original bytes reaching MainAI's
     own context if any earlier stage found a problem
```

### 4.1 "Unknown format = default deny/isolate", concretely

"Unknown" is defined structurally, not by absence of an extension: a format is UNKNOWN unless
stage 2's magic-byte verification matches an entry in a maintained, versioned allowlist of
format signatures the Safe Parser stage has a real parser for. A file whose claimed type is
`.docx` but whose magic bytes don't match a real OOXML zip structure is UNKNOWN (not
"corrupted docx, try anyway") — it never reaches stage 3, it stays quarantined, and a
`SecurityEvent` (`file_opened_massively`-adjacent, or a dedicated `format_mismatch` addition
to §2.1's vocabulary — flagged as a gap in the founder's given event-type list, worth adding
at implementation time) is emitted so the mesh has a durable record of the refusal. The
allowlist is versioned and part of Sentinel's own update lifecycle (§ "security
self-improvement" in the founder's Part R, covered in `MAINAI_V2_IMPLEMENTATION_PLAN.md`'s
dependency graph, not duplicated here).

---

## 5. Explicitly out of scope for this document

- The exact sandbox implementation technology (container/VM/language-level isolation) — an
  implementation-phase, platform-dependent decision (V2-J).
- Sentinel's own update/rule-lifecycle mechanics beyond the pointer above — covered fully
  under Part R in the implementation plan, not duplicated here to avoid drift.
- Whether Sentinel ships as a separate process/service — packaging question, V2-A §7 already
  flags this as open, restated here rather than re-litigated.

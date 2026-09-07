"""Dev Director -- future cross-layer integration seams (documentation only).

NO real logic lives here. This module exists to record the INTENDED future wiring points
for a later, separately-scoped round -- none of them are implemented, none of them are
imported by anything, and this module does not import any of the eight sibling V2 packages
or any Codex branch. Every seam below is a plain, unused typed stub or a comment describing
the intended composition -- exactly the same "document, do not wire" discipline
app.attachment_chamber.future_integration already established for this V2 lane.

== Guardian ==
A Program's pause/kill-switch (this round's own isolated stand-in, see the reconciliation
doc's #245-gap note) should eventually route through real app.guardian.evaluate_bounded_
action()/evaluate_containment_request() calls -- Guardian pausing a Program or revoking an
ExternalProviderLease is a real, future containment action, not built here. Same "ACTION
REQUEST != AUTHORITY, no real policy supplied -> raise" seam already proven by
app.operating_shell.risk's own Guardian-backed policy in its cross-layer tests.

== Sentinel ==
A BuilderResult/ExaminerVerdictRecord flagged as suspicious (a builder claiming completion
with obviously malicious diffs, or repeated collusion-check rejections from the same
identity) should eventually be able to become a real app.sentinel.SecurityEvent (event_type
likely AGENT_SCOPE_ESCALATION or a new dedicated provider-behavior type). Unexpected
filesystem/network behavior FROM an external provider adapter is Sentinel's domain, not
this package's -- this package only ever sees the adapter's DECLARED result, never its raw
execution behavior.

== Personal Recall (Codex's separate track) ==
Personal Recall may LATER read: job history, certification history, supersession, examiner
verdicts, Founder Briefs -- via read-only query functions over this package's own durable
state (once it has one -- this round is pure in-memory). RECALL RESULT != AUTHORITY: a
recalled past job/verdict is never itself treated as current authority for anything: a
CERTIFIED SHA from history does not re-certify a NEW SHA, and a past examiner identity does
not pre-approve a future one. No Codex branch is imported anywhere in this package.

== File Ingest Quarantine ==
Any file a provider adapter produces or references (an uploaded artifact, a generated
document) must pass app.attachment_chamber's real quarantine policy before this package (or
anything downstream of it) treats it as trusted -- PROVIDER-GENERATED FILE != TRUSTED FILE,
cited here by name, never imported. This package's own CompletionEvidence deliberately
carries only file PATHS/hashes (changed_files, test_commands), never raw file bytes or
content, so there is nothing here that would bypass that future quarantine gate even
accidentally.

== app.execution_envelopes / app.development_supervisor / app.workforce (real production) ==
A CERTIFIED Job's PullRequestProposal is, today, only ever a documented proposal -- actually
opening it, and actually merging it, must go through the REAL, EXISTING
app.execution_envelopes propose/authorize split and app.workforce's own risk-tiered
verification policy (this package's Builder/Examiner separation is a NEW, complementary
layer, not a replacement for either). Real local execution should keep using the REAL,
ALREADY-WORKING app.development_supervisor.run_authorized_goal_supervisor_tick() loop for
anything Level-0/Level-1-shaped; this package's own run_program_tick() is for the NEW
cross-goal, multi-provider, independently-examined coordination tier ABOVE it, never a
parallel reimplementation of what already runs in production.

== app.mainai_startup_readiness (real production) ==
AutonomyLevel's AUTONOMY_LEVEL_REQUIRED_READINESS mapping (see types.py) is DATA only this
round. A future real integration point would call the REAL
app.mainai_startup_readiness.evaluate_startup_readiness() and refuse to run any Job whose
Program autonomy level requires a ReadinessLevel the real system hasn't actually reached --
not built here, since that is a real production evaluation call, out of scope for this
isolated, unwired package.
"""

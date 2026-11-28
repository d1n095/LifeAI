# Life Case Engine — product requirements and architecture proposal
Date: 2026-11-28. Status: deferred design proposal, not implemented or authorized for immediate development. Preserve current roadmap and active agent assignments.

## Context and reuse
Extends Life OS Master Handoff (2026-07-16), sections 18 and 20: commerce, separate order/payment/fulfillment states, consumer rights, evidence timelines and disputes. Reuse existing MainAI orchestration, memory, scheduler and security kernel after verifying actual implementation. Relevant repository references: docs/MEMORY_ARCHITECTURE.md, docs/LIFE_EXECUTION_AUTHORIZATION_ENVELOPE.md, backend/app/project_memory.py. These are foundations, not proof of runtime enforcement. Do not create a competing autonomy plane or store personal cases in project memory.

## Product outcome
User can ask Life to resolve a missing delivery or unexpected subscription charge without supplying an order number, carrier or merchant login. Proactively discover anomalies and track cases until verified resolution. Generalize to insurance, payroll, invoices, debt disputes, warranties, public services, supplier and customer cases. Keep personal and business tenants separate.

## Architecture
Provider adapters (authorized email, documents, merchant portals, carrier APIs, bank feeds) -> normalized event ingestion -> entity reconciliation -> durable case state machine -> MainAI analysis/planning -> existing authorization and execution gates -> actions and verified outcomes.
Use deterministic rules and scheduled checks for routine monitoring; invoke AI only for ambiguity, interpretation and planning. Prefer existing durable job infrastructure; evaluate alternatives only if gaps are demonstrated. AfterShip and EasyPost are candidate tracking references, Open Banking a candidate transaction source; verify coverage, cost, privacy and permissions before selection. Prefer direct carrier integrations where practical; no vendor lock-in.

## Data and workflow
Separate order, shipment, payment, contract/subscription and case entities. Support multiple shipments and payments per order, partial delivery, refunds, unknown identifiers and disputed facts. Link with confidence and provenance; never silently merge ambiguous matches.
Track order placed/confirmed/cancelled; shipment unknown/label created/carrier accepted/in transit/delivered/exception/returned; payment attempted/pending/settled/reversed/refunded/disputed; subscription trial/active/cancellation requested/cancelled/unknown. Carrier-reported delivery is not proof of personal receipt. No tracking data is not proof of nonshipment.
Case lifecycle: detected -> investigating -> planned -> approval if required -> executing -> awaiting response -> verifying -> resolved or escalated. Reopen on new evidence. Persist next action, deadline, retry policy, owner, state version, evidence and audit references. Support idempotency, deduplication, outbox, backoff, restart recovery, concurrency control and manual override. Never repeat external submissions blindly after uncertain outcomes.

## Consumer workflow
Discover order confirmations, merchant identity, order/tracking IDs, offer terms, invoices, transactions and correspondence from authorized sources. Identify carrier and verify actual handoff versus label creation. Compare promised delivery, payment and contract terms; detect unexpected renewals, price changes, duplicate charges, delays and missing deliveries. Obtain evidence or request it when unavailable. Determine jurisdiction and applicable law version from authoritative sources; distinguish user statements, third-party claims, verified facts and inference. Calculate deadlines with source and uncertainty. Draft or send authorized inquiries, cancellations, complaints, disputes and refund requests; follow replies, verify refund settlement and cancellation, escalate appropriately. Missing login must not block alternative lawful contact routes. No fabricated evidence or legal guarantees.

## Security and privacy
Local-first encrypted case/evidence storage, tenant isolation, least privilege, read-only connectors by default, scoped short-lived credentials in vault, explicit retention/deletion/export policy. Preserve originals and provenance; hashes help detect modification but do not prove truth. Treat emails, webpages and attachments as untrusted data; isolate parsing, prevent prompt injection, SSRF and credential leakage. Existing authorization envelope is a strict ceiling; recheck permissions at execution, not only planning. Require explicit or policy-defined approval for external communications and higher-risk actions; step-up for financial actions, binding settlements, sensitive disclosure or identity verification. Never bypass authentication, CAPTCHA or bank controls. No automatic payment blocking or dispute submission without supported integration and authorization. Redact logs; never publish personal case evidence to public GitHub. Learn from outcomes without self-expanding permissions.

## Acceptance scenarios
1. Synthetic free trial, no receipt, attempted charge, no merchant login: discover available evidence, flag uncertainty, prepare dispute, monitor response and verify closure.
2. Label created but no carrier acceptance: do not claim shipment.
3. Carrier says delivered, user denies receipt: retain both claims and request proof.
4. Partial delivery and refund, multiple orders with same amount: reconcile without false matching.
5. Restart during waiting period: resume without duplicate email or lost deadline.
6. Forged email or prompt injection: no unauthorized action or secret disclosure.
7. Expired authorization, cross-tenant access, unavailable bank API: fail closed and report concrete blocker.
8. Merchant promises refund but bank has not settled: keep case open.
9. Deadline or law uncertain: surface uncertainty and escalate before deadline where possible.

## Delivery sequence (deferred)
Inventory existing code and open PRs first; map reusable components and gaps. Phase 1 read-only ingestion, evidence, state and synthetic tests. Phase 2 monitoring, reconciliation, durable follow-up. Phase 3 authorized communications and verification. Phase 4 optional regulated financial integrations and wider domains. Require security review, integration tests, restart/idempotency tests and provider-specific validation before production. No change to active priorities, no agent reassignment, no implementation claim.

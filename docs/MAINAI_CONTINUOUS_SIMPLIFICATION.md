# MAINAI Continuous Simplification (Stage F)

**Branch:** `cursor/mainai-continuous-simplification`  
**Depends on:** Stage E (#213)

## Purpose

First measurable simplification layer. Detects:

- duplicate concepts / workflows
- orphan memory
- temporary architecture markers

MainAI may **propose** simplification. Stage F never auto-applies.

## Hard rule

Proposals that touch `security` / `authority` / `audit` / `recovery` (and related) are
flagged `protected=True` and `auto_apply_allowed=False`. Simplification must not remove
those surfaces.

"""Context resolution for referring expressions (MainAI V2, Stage V2-I4/I6 per the founder's
body-section numbering).

DO NOT silently guess if multiple high-confidence targets exist: resolve_reference() returns
an AmbiguousResolution whenever more than one candidate is plausible, never an arbitrary pick.
"""

from __future__ import annotations

from app.operating_shell.types import (
    AmbiguousResolution,
    ReferenceKind,
    ResolvedReference,
    WorkspaceContext,
    WorkspaceTarget,
)


def resolve_reference(
    kind: ReferenceKind, context: WorkspaceContext
) -> ResolvedReference | AmbiguousResolution:
    """Resolves a closed set of referring-expression categories against current context.
    THIS/THAT resolve via focus/selection; THE_OLD_ONE, RECENT_FILE, REOPEN_LAST resolve via
    known_targets + recent_action_refs; CONTINUE resolves via active_intent_id (the caller
    is expected to separately look up the actual IntentObject -- this function only reports
    whether a single active intent reference exists in context, not which one exactly, since
    that lookup lives in intent.py). COMPARE_TARGETS requires at least two selections/targets.
    SHOW_REASONING is handled by evidence.py, not here."""
    if kind == ReferenceKind.THIS:
        if context.focus is not None and context.focus.focused_target_ref is not None:
            target = _find_target(context, context.focus.focused_target_ref)
            if target is not None:
                return ResolvedReference(reference_kind=kind, target=target)
        return AmbiguousResolution(reference_kind=kind, candidates=(), reason="nothing is currently focused")

    if kind == ReferenceKind.THAT:
        # "that" = something recently referenced but NOT the current focus.
        candidates = [
            _find_target(context, ref) for ref in context.recent_action_refs
            if ref != (context.focus.focused_target_ref if context.focus else None)
        ]
        candidates = [c for c in candidates if c is not None]
        return _single_or_ambiguous(kind, candidates, "no prior non-focused reference exists")

    if kind == ReferenceKind.THE_OLD_ONE:
        # Ambiguous by construction unless exactly one known target is explicitly marked
        # (via title containing a version/age marker) -- this foundation stage does not
        # attempt real recency/age inference, so with >=2 known targets this is always
        # ambiguous rather than guessed.
        if len(context.known_targets) == 1:
            return ResolvedReference(reference_kind=kind, target=context.known_targets[0])
        return AmbiguousResolution(
            reference_kind=kind, candidates=tuple(context.known_targets),
            reason="cannot determine which target is 'the old one' without explicit disambiguation",
        )

    if kind == ReferenceKind.RECENT_FILE:
        candidates = [t for t in context.known_targets if t.kind == "document"]
        return _single_or_ambiguous(kind, candidates, "no recent file reference exists")

    if kind == ReferenceKind.REOPEN_LAST:
        if context.recent_action_refs:
            target = _find_target(context, context.recent_action_refs[-1])
            if target is not None:
                return ResolvedReference(reference_kind=kind, target=target)
        return AmbiguousResolution(reference_kind=kind, candidates=(), reason="no recent action to reopen")

    if kind == ReferenceKind.COMPARE_TARGETS:
        if context.selection is None:
            return AmbiguousResolution(reference_kind=kind, candidates=(), reason="no selection to compare")
        # A genuine multi-item selection is required; this package models a single
        # WorkspaceSelection per context, so "compare these" needs >= 2 known_targets that
        # are currently selected-shaped -- otherwise ambiguous (never invent a 2nd target).
        selected_like = [t for t in context.known_targets if t.kind in ("file", "document", "selection")]
        if len(selected_like) < 2:
            return AmbiguousResolution(reference_kind=kind, candidates=tuple(selected_like), reason="comparison requires at least two selected items")
        return AmbiguousResolution(reference_kind=kind, candidates=tuple(selected_like), reason="multiple plausible comparison sets exist")

    if kind == ReferenceKind.CONTINUE:
        if context.active_intent_id is None:
            return AmbiguousResolution(reference_kind=kind, candidates=(), reason="no active intent in context")
        return ResolvedReference(
            reference_kind=kind, target=WorkspaceTarget(target_id=context.active_intent_id, kind="intent", title="active intent")
        )

    if kind == ReferenceKind.SHOW_REASONING:
        return AmbiguousResolution(reference_kind=kind, candidates=(), reason="SHOW_REASONING is resolved via evidence.py, not context.py")

    raise ValueError(f"unrecognized ReferenceKind: {kind!r}")


def _find_target(context: WorkspaceContext, target_id) -> WorkspaceTarget | None:
    for t in context.known_targets:
        if t.target_id == target_id:
            return t
    return None


def _single_or_ambiguous(kind: ReferenceKind, candidates: list[WorkspaceTarget], empty_reason: str):
    if not candidates:
        return AmbiguousResolution(reference_kind=kind, candidates=(), reason=empty_reason)
    if len(candidates) > 1:
        return AmbiguousResolution(reference_kind=kind, candidates=tuple(candidates), reason=f"{len(candidates)} plausible candidates")
    return ResolvedReference(reference_kind=kind, target=candidates[0])

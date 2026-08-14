"""Manual and deterministic Memory Thread operations; no semantic or provider calls."""

import uuid
from collections import defaultdict, deque
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.active_context.service import (
    InvalidContextReference,
    _edges,
    _owned_row,
    _require_ref,
    pin_object,
)
from app.models.memory_thread import (
    MemoryThread,
    MemoryThreadEvent,
    MemoryThreadMember,
    MemoryThreadRelationship,
)
from app.models.user import User


class InvalidThreadOperation(ValueError):
    pass


def _thread(
    db: Session, owner_id: uuid.UUID, thread_id: uuid.UUID, *, lock: bool = False
) -> MemoryThread:
    query = select(MemoryThread).where(
        MemoryThread.id == thread_id, MemoryThread.owner_id == owner_id
    )
    row = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if row is None:
        raise InvalidThreadOperation("thread is missing or belongs to another owner")
    return row


def _event(
    db: Session,
    thread: MemoryThread,
    event_type: str,
    *,
    member_id=None,
    relationship_id=None,
    actor_type="unknown",
    detail=None,
) -> None:
    db.add(
        MemoryThreadEvent(
            thread_id=thread.id,
            owner_id=thread.owner_id,
            member_id=member_id,
            relationship_id=relationship_id,
            event_type=event_type,
            actor_type=actor_type,
            detail=detail or {},
        )
    )


def _source_time(row: object):
    for name in (
        "occurred_at",
        "created_at",
        "observed_at",
        "ingested_at",
        "uploaded_at",
        "timestamp",
    ):
        value = getattr(row, name, None)
        if isinstance(value, datetime):
            return value, "source"
    return None, "unknown"


def create_thread(
    db: Session,
    *,
    owner_id: uuid.UUID,
    idempotency_key: str,
    manual_label: str | None = None,
    system_label: str | None = None,
    classification_basis: str = "unknown",
) -> MemoryThread:
    # Serialize first creation by owner so concurrent replay converges before the unique
    # constraint is reached. This works across processes and avoids an aborted loser session.
    owner_exists = db.execute(
        select(User.id).where(User.id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if owner_exists is None:
        raise InvalidThreadOperation("owner does not exist")
    existing = db.execute(
        select(MemoryThread).where(
            MemoryThread.owner_id == owner_id,
            MemoryThread.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    semantic = (manual_label, system_label, classification_basis)
    if existing:
        if (
            existing.manual_label,
            existing.system_label,
            existing.classification_basis,
        ) != semantic:
            raise InvalidThreadOperation(
                "idempotency key reused for a different thread"
            )
        return existing
    thread = MemoryThread(
        owner_id=owner_id,
        idempotency_key=idempotency_key,
        manual_label=manual_label,
        system_label=system_label,
        classification_basis=classification_basis,
    )
    db.add(thread)
    db.flush()
    _event(
        db,
        thread,
        "thread_created",
        actor_type="founder" if classification_basis == "manual" else "system",
        detail={
            "manual_label": manual_label,
            "system_label": system_label,
            "basis": classification_basis,
        },
    )
    return thread


def add_member(
    db: Session,
    *,
    owner_id: uuid.UUID,
    thread_id: uuid.UUID,
    member_kind: str,
    member_ref_id: str | uuid.UUID,
    membership_basis: str = "unknown",
    classification_basis: str = "unknown",
    provenance: dict | None = None,
    idempotency_key: str | None = None,
    actor_type: str = "unknown",
) -> MemoryThreadMember:
    thread = _thread(db, owner_id, thread_id, lock=True)
    try:
        ref, row = _require_ref(db, owner_id, member_kind, member_ref_id)
    except InvalidContextReference as exc:
        raise InvalidThreadOperation(str(exc)) from exc
    ref_id = str(ref.object_id)
    if idempotency_key:
        idem = db.execute(
            select(MemoryThreadMember).where(
                MemoryThreadMember.owner_id == owner_id,
                MemoryThreadMember.thread_id == thread.id,
                MemoryThreadMember.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if idem:
            if (
                idem.member_kind,
                idem.member_ref_id,
                idem.membership_basis,
                idem.classification_basis,
                idem.provenance,
            ) != (
                member_kind,
                ref_id,
                membership_basis,
                classification_basis,
                provenance or {},
            ):
                raise InvalidThreadOperation(
                    "idempotency key reused for different membership"
                )
            return idem
    existing = db.execute(
        select(MemoryThreadMember).where(
            MemoryThreadMember.thread_id == thread.id,
            MemoryThreadMember.member_kind == member_kind,
            MemoryThreadMember.member_ref_id == ref_id,
        )
    ).scalar_one_or_none()
    now = datetime.utcnow()
    if existing:
        existing.last_seen_at = now
        if existing.state != "active":
            existing.state = "active"
            _event(
                db,
                thread,
                "member_added",
                member_id=existing.id,
                actor_type=actor_type,
                detail={"reactivated": True},
            )
        return existing
    occurred_at, time_basis = _source_time(row)
    member = MemoryThreadMember(
        thread_id=thread.id,
        owner_id=owner_id,
        member_kind=member_kind,
        member_ref_id=ref_id,
        membership_basis=membership_basis,
        classification_basis=classification_basis,
        provenance=provenance or {},
        source_occurred_at=occurred_at,
        source_time_basis=time_basis,
        idempotency_key=idempotency_key,
        added_at=now,
        last_seen_at=now,
    )
    db.add(member)
    db.flush()
    thread.last_activity_at = now
    _event(
        db,
        thread,
        "member_added",
        member_id=member.id,
        actor_type=actor_type,
        detail={
            "member_kind": member_kind,
            "member_ref_id": ref_id,
            "basis": membership_basis,
        },
    )
    return member


def deactivate_member(
    db: Session,
    *,
    owner_id: uuid.UUID,
    thread_id: uuid.UUID,
    member_kind: str,
    member_ref_id: str | uuid.UUID,
    reason: str,
    actor_type: str = "founder",
) -> MemoryThreadMember:
    if not reason.strip():
        raise InvalidThreadOperation("deactivation requires a reason")
    thread = _thread(db, owner_id, thread_id, lock=True)
    member = db.execute(
        select(MemoryThreadMember)
        .where(
            MemoryThreadMember.thread_id == thread.id,
            MemoryThreadMember.member_kind == member_kind,
            MemoryThreadMember.member_ref_id == str(uuid.UUID(str(member_ref_id))),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if member is None:
        raise InvalidThreadOperation("membership not found")
    if member.state != "inactive":
        member.state = "inactive"
        _event(
            db,
            thread,
            "member_deactivated",
            member_id=member.id,
            actor_type=actor_type,
            detail={"reason": reason},
        )
    return member


def add_relationship(
    db: Session,
    *,
    owner_id: uuid.UUID,
    from_thread_id: uuid.UUID,
    to_thread_id: uuid.UUID,
    relationship_type: str,
    basis: str = "unknown",
    provenance: dict | None = None,
    idempotency_key: str | None = None,
    actor_type: str = "unknown",
) -> MemoryThreadRelationship:
    if from_thread_id == to_thread_id:
        raise InvalidThreadOperation("a thread cannot relate to itself")
    first, second = sorted((from_thread_id, to_thread_id), key=str)
    locked = list(
        db.execute(
            select(MemoryThread)
            .where(
                MemoryThread.owner_id == owner_id, MemoryThread.id.in_([first, second])
            )
            .order_by(MemoryThread.id)
            .with_for_update()
        ).scalars()
    )
    if len(locked) != 2:
        raise InvalidThreadOperation(
            "relationship thread is missing or belongs to another owner"
        )
    if idempotency_key:
        idem = db.execute(
            select(MemoryThreadRelationship).where(
                MemoryThreadRelationship.owner_id == owner_id,
                MemoryThreadRelationship.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if idem:
            if (
                idem.from_thread_id,
                idem.to_thread_id,
                idem.relationship_type,
                idem.basis,
                idem.provenance,
            ) != (
                from_thread_id,
                to_thread_id,
                relationship_type,
                basis,
                provenance or {},
            ):
                raise InvalidThreadOperation(
                    "idempotency key reused for different relationship"
                )
            return idem
    existing = db.execute(
        select(MemoryThreadRelationship).where(
            MemoryThreadRelationship.owner_id == owner_id,
            MemoryThreadRelationship.from_thread_id == from_thread_id,
            MemoryThreadRelationship.to_thread_id == to_thread_id,
            MemoryThreadRelationship.relationship_type == relationship_type,
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    relation = MemoryThreadRelationship(
        owner_id=owner_id,
        from_thread_id=from_thread_id,
        to_thread_id=to_thread_id,
        relationship_type=relationship_type,
        basis=basis,
        provenance=provenance or {},
        idempotency_key=idempotency_key,
    )
    db.add(relation)
    db.flush()
    source = next(t for t in locked if t.id == from_thread_id)
    _event(
        db,
        source,
        "relation_added",
        relationship_id=relation.id,
        actor_type=actor_type,
        detail={
            "to_thread_id": str(to_thread_id),
            "relationship_type": relationship_type,
        },
    )
    return relation


def expand_thread(
    db: Session,
    *,
    owner_id: uuid.UUID,
    thread_id: uuid.UUID,
    anchor_kind: str,
    anchor_ref_id: str | uuid.UUID,
    max_depth: int = 3,
    max_members: int = 100,
    max_per_type: int = 30,
) -> list[MemoryThreadMember]:
    if not (
        0 <= max_depth <= 10 and 1 <= max_members <= 500 and 1 <= max_per_type <= 200
    ):
        raise InvalidThreadOperation("expansion bounds outside supported limits")
    _thread(db, owner_id, thread_id, lock=True)
    try:
        root, row = _require_ref(db, owner_id, anchor_kind, anchor_ref_id)
    except InvalidContextReference as exc:
        raise InvalidThreadOperation(str(exc)) from exc
    queue = deque(
        [
            (
                root,
                row,
                0,
                [
                    {
                        "kind": root.object_type,
                        "ref": str(root.object_id),
                        "relation": "anchor",
                    }
                ],
            )
        ]
    )
    visited: set[tuple[str, str]] = set()
    counts: defaultdict[str, int] = defaultdict(int)
    while queue and len(visited) < max_members:
        ref, current, depth, path = queue.popleft()
        key = (ref.object_type, str(ref.object_id))
        if key in visited or counts[ref.object_type] >= max_per_type:
            continue
        visited.add(key)
        counts[ref.object_type] += 1
        add_member(
            db,
            owner_id=owner_id,
            thread_id=thread_id,
            member_kind=ref.object_type,
            member_ref_id=ref.object_id,
            membership_basis="deterministic_relationship",
            classification_basis="deterministic",
            provenance={"activation_path": path},
            actor_type="deterministic_resolver",
        )
        if depth < max_depth:
            for edge in _edges(db, owner_id, ref, current):
                target = _owned_row(db, owner_id, edge.target)
                if target is not None:
                    queue.append(
                        (
                            edge.target,
                            target,
                            depth + 1,
                            [
                                *path,
                                {
                                    "kind": edge.target.object_type,
                                    "ref": str(edge.target.object_id),
                                    "relation": edge.relation,
                                },
                            ],
                        )
                    )
    return thread_members(db, owner_id=owner_id, thread_id=thread_id)


def merge_threads(
    db: Session,
    *,
    owner_id: uuid.UUID,
    source_thread_id: uuid.UUID,
    target_thread_id: uuid.UUID,
    idempotency_key: str,
) -> MemoryThread:
    relation = add_relationship(
        db,
        owner_id=owner_id,
        from_thread_id=source_thread_id,
        to_thread_id=target_thread_id,
        relationship_type="merged_into",
        basis="manual",
        provenance={"operation": "merge"},
        idempotency_key=idempotency_key,
        actor_type="founder",
    )
    source, target = (
        _thread(db, owner_id, source_thread_id, lock=True),
        _thread(db, owner_id, target_thread_id, lock=True),
    )
    for member in thread_members(db, owner_id=owner_id, thread_id=source.id):
        if member.state == "active":
            add_member(
                db,
                owner_id=owner_id,
                thread_id=target.id,
                member_kind=member.member_kind,
                member_ref_id=member.member_ref_id,
                membership_basis="continuation",
                classification_basis="deterministic",
                provenance={
                    "merged_from": str(source.id),
                    "source_member_id": str(member.id),
                },
                actor_type="founder",
            )
    if source.state != "superseded":
        source.state = "superseded"
        _event(
            db,
            source,
            "merged",
            relationship_id=relation.id,
            actor_type="founder",
            detail={"target": str(target.id)},
        )
        _event(
            db,
            source,
            "superseded",
            actor_type="founder",
            detail={"target": str(target.id)},
        )
    return target


def branch_thread(
    db: Session,
    *,
    owner_id: uuid.UUID,
    parent_thread_id: uuid.UUID,
    idempotency_key: str,
    manual_label: str | None = None,
    member_ids: list[uuid.UUID] | None = None,
) -> MemoryThread:
    parent = _thread(db, owner_id, parent_thread_id)
    child = create_thread(
        db,
        owner_id=owner_id,
        idempotency_key=idempotency_key,
        manual_label=manual_label,
        classification_basis="manual" if manual_label else "unknown",
    )
    relation = add_relationship(
        db,
        owner_id=owner_id,
        from_thread_id=parent.id,
        to_thread_id=child.id,
        relationship_type="branch",
        basis="manual",
        provenance={"operation": "branch"},
        idempotency_key=f"{idempotency_key}:relation",
        actor_type="founder",
    )
    selected = set(member_ids or [])
    for member in thread_members(db, owner_id=owner_id, thread_id=parent.id):
        if member.id in selected:
            add_member(
                db,
                owner_id=owner_id,
                thread_id=child.id,
                member_kind=member.member_kind,
                member_ref_id=member.member_ref_id,
                membership_basis="continuation",
                classification_basis="manual",
                provenance={
                    "branched_from": str(parent.id),
                    "source_member_id": str(member.id),
                },
                actor_type="founder",
            )
    _event(
        db,
        child,
        "branched",
        relationship_id=relation.id,
        actor_type="founder",
        detail={"parent": str(parent.id)},
    )
    return child


def update_thread_label(
    db: Session,
    *,
    owner_id: uuid.UUID,
    thread_id: uuid.UUID,
    label: str,
    basis: str = "manual",
) -> MemoryThread:
    if not label.strip():
        raise InvalidThreadOperation("label cannot be empty")
    thread = _thread(db, owner_id, thread_id, lock=True)
    old = {
        "manual_label": thread.manual_label,
        "system_label": thread.system_label,
        "classification_basis": thread.classification_basis,
    }
    if basis == "manual":
        thread.manual_label = label
    else:
        thread.system_label = label
    thread.classification_basis = basis
    _event(
        db,
        thread,
        "label_changed",
        actor_type="founder" if basis == "manual" else "system",
        detail={"old": old, "new_label": label, "basis": basis},
    )
    return thread


def update_thread_state(
    db: Session, *, owner_id: uuid.UUID, thread_id: uuid.UUID, state: str, reason: str
) -> MemoryThread:
    if not reason.strip():
        raise InvalidThreadOperation("state change requires a reason")
    thread = _thread(db, owner_id, thread_id, lock=True)
    old = thread.state
    thread.state = state
    _event(
        db,
        thread,
        "state_changed",
        actor_type="founder",
        detail={"old": old, "new": state, "reason": reason},
    )
    return thread


def thread_members(
    db: Session,
    *,
    owner_id: uuid.UUID,
    thread_id: uuid.UUID,
    include_inactive: bool = True,
) -> list[MemoryThreadMember]:
    _thread(db, owner_id, thread_id)
    query = select(MemoryThreadMember).where(
        MemoryThreadMember.thread_id == thread_id,
        MemoryThreadMember.owner_id == owner_id,
    )
    if not include_inactive:
        query = query.where(MemoryThreadMember.state == "active")
    return list(
        db.execute(
            query.order_by(
                MemoryThreadMember.source_occurred_at.asc().nullslast(),
                MemoryThreadMember.added_at,
                MemoryThreadMember.id,
            )
        ).scalars()
    )


def link_thread_to_context(
    db: Session, *, owner_id: uuid.UUID, thread_id: uuid.UUID, context_set_id: uuid.UUID
):
    _thread(db, owner_id, thread_id)
    return pin_object(
        db,
        owner_id=owner_id,
        context_set_id=context_set_id,
        object_type="memory_thread",
        object_ref=thread_id,
        event_detail={"bridge": "memory_thread_reference_only"},
    )

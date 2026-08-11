"""MainAI V0.3 -- minimal, real engineering-lesson conflict detection. Confirmed absent in
V0.1 (EngineeringLessonConfidence's own docstring: "nothing yet computes lesson-vs-lesson
contradiction"). Before this, app/mainai_execution/lessons.py's apply_lessons_to_verification_
plan() would silently apply EVERY active lesson matching a task_type's tags, one after another
-- including two lessons that actively disagree about the same affected_component, with no
signal that a founder should look at either one again.

Deliberately two-stage, matching this codebase's own "narrow deterministically, judge only the
real candidates" convention (e.g. app/mainai_execution/recovery_classifier.py's evidence-first
shape):
  1. find_conflict_candidate_pairs(): a pure, deterministic, non-AI narrowing -- only lessons
     that are BOTH `active`, share the SAME `affected_component`, and share AT LEAST ONE
     `applies_to` tag are even considered a plausible conflicting pair. Two lessons about
     unrelated components are never compared at all.
  2. detect_conflict(): the one genuinely judgment-requiring step -- a real AI call (same
     provider chain every other judgment call in this codebase already uses,
     chat_with_fallback()) asked whether two candidate lessons' `general_rule`/`fix` actually
     contradict each other for the same component, not just both being relevant to it.

Fails closed the way this codebase's other fail-closed defaults do: a malformed or errored AI
response is treated as NOT a proven conflict (never silently marks two lessons `disputed` on an
unreliable signal) -- but IS logged, so a real underlying problem in the judgment call itself is
visible as an operational issue, not silently swallowed. mark_conflict() never picks a winner:
BOTH lessons move to `disputed`, preserving their full history (no delete-based forgetting,
matching this table's other status transitions)."""

import json
import logging
import uuid

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models.mainai_execution import EngineeringLesson, EngineeringLessonStatus, MainAITask, MainAITaskEvent, MainAITaskEventType
from app.providers.base import Message
from app.providers.registry import chat_with_fallback

logger = logging.getLogger("mainai.execution")

LESSON_CONFLICT_SYSTEM_PROMPT = (
    "Du är MainAI:s granskare av tekniska lärdomar (engineering lessons). Du får TVÅ lärdomar "
    "om samma komponent. Svara ENDAST med ett JSON-objekt på formen "
    '{"conflict": true|false, "reasoning": "..."}. `conflict` ska vara true ENDAST om de två '
    "lärdomarnas `general_rule`/`fix` uttryckligen MOTSÄGER varandra (t.ex. en säger \"gör X\", "
    "den andra säger \"gör aldrig X\") -- inte bara att de båda handlar om samma komponent eller "
    "kompletterar varandra. Var konservativ: om du är osäker, svara false."
)


def find_conflict_candidate_pairs(db: Session, *, lessons: list[EngineeringLesson]) -> list[tuple[EngineeringLesson, EngineeringLesson]]:
    """Pure, deterministic pairing among an already-fetched candidate set (e.g. lookup_lessons()'s
    own result for one task_type) -- no separate database query, so this is safe to call on
    every planning pass without extra I/O. Never pairs a lesson with itself, never pairs the
    same two lessons twice, never considers a lesson that isn't `active` (a `disputed`/
    `superseded`/`historical` lesson has already been resolved or is no longer live guidance)."""
    active = [lesson for lesson in lessons if lesson.status == EngineeringLessonStatus.active]
    pairs: list[tuple[EngineeringLesson, EngineeringLesson]] = []
    for i, a in enumerate(active):
        for b in active[i + 1 :]:
            if a.affected_component != b.affected_component:
                continue
            if not (set(a.applies_to) & set(b.applies_to)):
                continue
            pairs.append((a, b))
    return pairs


async def detect_conflict(db: Session, *, lesson_a: EngineeringLesson, lesson_b: EngineeringLesson) -> tuple[bool, str]:
    """Returns (is_conflict, reasoning). The one real AI-judgment call in this module -- see
    module docstring for the fail-closed default on a malformed/errored response."""
    prompt = (
        f"## Lärdom A (id={lesson_a.id})\nKomponent: {lesson_a.affected_component}\n"
        f"Regel: {lesson_a.general_rule}\nÅtgärd: {lesson_a.fix}\n\n"
        f"## Lärdom B (id={lesson_b.id})\nKomponent: {lesson_b.affected_component}\n"
        f"Regel: {lesson_b.general_rule}\nÅtgärd: {lesson_b.fix}"
    )
    messages = [Message(role="system", content=LESSON_CONFLICT_SYSTEM_PROMPT), Message(role="user", content=prompt)]
    try:
        result, _attempted = await chat_with_fallback(db, messages)
        raw = result.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[len("json") :]
            raw = raw.strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or "conflict" not in parsed:
            raise ValueError(f"malformed conflict-judgment response: {parsed!r}")
        return bool(parsed["conflict"]), str(parsed.get("reasoning", ""))
    except Exception as exc:  # noqa: BLE001 -- fail closed: an unreliable judgment is never treated as a proven conflict
        logger.warning("Lesson conflict judgment failed for lessons %s/%s: %s", lesson_a.id, lesson_b.id, exc)
        return False, ""


def _tasks_that_applied_lesson(db: Session, lesson_id: uuid.UUID) -> list[MainAITask]:
    """Tasks whose `created` MainAITaskEvent recorded `lesson_id` in its `lessons_applied` list
    -- see app/mainai_execution/lessons.py's apply_lessons_to_verification_plan(), which is the
    ONLY place that list is ever written. This is the durable link between an abstract,
    founder-wide lesson and the concrete tasks it actually influenced, used below to know which
    tasks a newly-detected conflict is actionable evidence for. A jsonb containment check
    (`detail @> {"lessons_applied": [lesson_id]}`) rather than a Python-side scan, since a
    founder-wide lesson can in principle have been applied to many tasks across many goals.
    `cast()`'s target type also drives how the literal is bound -- passing an already
    json.dumps()'d STRING here would get serialized a second time (a JSON string containing
    JSON text, never matching anything), so the raw dict is passed and JSONB's own bind
    processor does the one real serialization."""
    containment = cast({"lessons_applied": [str(lesson_id)]}, JSONB)
    task_ids = (
        db.execute(
            select(MainAITaskEvent.task_id).where(
                MainAITaskEvent.event_type == MainAITaskEventType.created,
                cast(MainAITaskEvent.detail, JSONB).op("@>")(containment),
            )
        )
        .scalars()
        .all()
    )
    if not task_ids:
        return []
    return db.execute(select(MainAITask).where(MainAITask.id.in_(task_ids))).scalars().all()


def mark_conflict(db: Session, *, lesson_a: EngineeringLesson, lesson_b: EngineeringLesson, reasoning: str) -> None:
    """Moves BOTH lessons to `disputed` -- never picks a winner. A founder must explicitly
    resolve which (if either) returns to `active`; nothing in this codebase does that
    automatically.

    Also records a `lesson_conflict_detected` MainAITaskEvent on every task whose verification
    plan actually used one of these two lessons (via _tasks_that_applied_lesson() above) --
    migration 0036 added this event type specifically for this purpose. A lesson that was
    disputed but never actually applied to any task simply produces no events here; nothing
    forces one."""
    lesson_a.status = EngineeringLessonStatus.disputed
    lesson_b.status = EngineeringLessonStatus.disputed
    db.add(lesson_a)
    db.add(lesson_b)
    for lesson, other in ((lesson_a, lesson_b), (lesson_b, lesson_a)):
        for task in _tasks_that_applied_lesson(db, lesson.id):
            db.add(
                MainAITaskEvent(
                    task_id=task.id,
                    owner_id=task.owner_id,
                    event_type=MainAITaskEventType.lesson_conflict_detected,
                    detail={"lesson_id": str(lesson.id), "conflicting_lesson_id": str(other.id), "reasoning": reasoning},
                )
            )
    db.flush()


async def resolve_conflicts_among(db: Session, *, lessons: list[EngineeringLesson]) -> list[EngineeringLesson]:
    """The one entry point callers actually use: given a candidate lesson list (e.g. lookup_
    lessons()'s result for a task_type), finds and resolves every genuine conflict among them,
    and returns the lessons that are STILL `active` afterward -- safe for a caller like
    apply_lessons_to_verification_plan() to keep using without also re-implementing the
    filtering. Lessons this call newly disputes are excluded from the returned list; lessons
    already `disputed` from an earlier pass were never in the input (lookup_lessons() only
    returns `active` rows) so there's nothing to double-resolve."""
    pairs = find_conflict_candidate_pairs(db, lessons=lessons)
    disputed_ids: set = set()
    for lesson_a, lesson_b in pairs:
        if lesson_a.id in disputed_ids or lesson_b.id in disputed_ids:
            continue  # already resolved via an earlier pair this same call
        is_conflict, reasoning = await detect_conflict(db, lesson_a=lesson_a, lesson_b=lesson_b)
        if is_conflict:
            mark_conflict(db, lesson_a=lesson_a, lesson_b=lesson_b, reasoning=reasoning)
            disputed_ids.add(lesson_a.id)
            disputed_ids.add(lesson_b.id)
    return [lesson for lesson in lessons if lesson.id not in disputed_ids]

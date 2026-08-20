"""Wire `capability_record` into `app.active_context.service`'s central object-reference
registry.

Adversarial cross-stack review of migrations 0048-0050 (see
docs/LIFE_COGNITION_FOUNDATION_REVIEW_2026-08-18.md) found: `founder_memory_note` (0049) and
`diagnosis_record` (0050) were both wired into this registry when they were built, but
`capability_record` (0048, built first) never was -- an undocumented inconsistency, not a
deliberate design choice (docs/LIFE_CAPABILITY_REALITY.md never mentions the omission at
all). A capability gap discovered while working a specific goal/task currently cannot be
linked (via `active_context`/`memory_threads`) to the task that discovered it, unlike a
founder memory note or a diagnosis -- this migration closes exactly that gap, no more.

Same mechanism, never a new linking system: widens the same three CHECK constraints already
widened twice (migrations 0049, 0050) by one more value each. No new table, no new column."""

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
        ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record','capability_record','explicit_topic'
        ));
        ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
        ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record','capability_record'
        ));
        ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
        ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','active_context_set',
            'life_intent','life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record','capability_record'
        ));
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE memory_thread_members DROP CONSTRAINT ck_memory_thread_member_kind;
        ALTER TABLE memory_thread_members ADD CONSTRAINT ck_memory_thread_member_kind CHECK(member_kind IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','active_context_set',
            'life_intent','life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record'
        ));
        ALTER TABLE active_context_members DROP CONSTRAINT ck_active_context_member_object_type;
        ALTER TABLE active_context_members ADD CONSTRAINT ck_active_context_member_object_type CHECK(object_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record'
        ));
        ALTER TABLE active_context_sets DROP CONSTRAINT ck_active_context_set_anchor_type;
        ALTER TABLE active_context_sets ADD CONSTRAINT ck_active_context_set_anchor_type CHECK(anchor_type IN (
            'conversation','message','document','knowledge_version','knowledge_claim','memory_source_unit',
            'document_source_unit','message_source_unit','mainai_goal','mainai_plan','mainai_task','mainai_job',
            'mainai_checkpoint','mainai_recovery','engineering_lesson','intelligence_execution','intelligence_evidence',
            'intelligence_interpretation','intelligence_idea','project','project_note','memory_thread','life_intent',
            'life_intent_blocker','life_problem','life_problem_approach','life_solution_component',
            'life_problem_assumption','life_problem_decision','life_approach_outcome','founder_memory_note',
            'diagnosis_record','explicit_topic'
        ));
    """)

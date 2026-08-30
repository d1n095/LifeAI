# MainAI — Concept / Idea Reconciliation (Stage B)

Deterministic, provider-free reconciliation over existing `project_entities`.

## Invariant

Differently-worded versions of the **same** idea resolve to **one** canonical
`ProjectEntity` and do **not** spawn a second `work_candidate`.

## Mechanism

| Piece | Role |
|---|---|
| `normalize_concept_text` | NFKC + casefold + strip punct + collapse space |
| `project_entities.title_normalized` | Fingerprint; partial unique among current rows |
| `project_entity_aliases` | Inspectable alternate surface forms |
| `promote_interpretation_proposal` SAME collapse | Reuses canonical entity; no duplicate WC |
| Relationship vocabulary | `same` (collapse only), `partial_overlap`, `related`, `depends_on`, `contradicts`, `supersedes`, `extends`, `alternative`, `reuses` (+ legacy) |

Memory mutation does not create broader authority.

## Migration

`0064_concept_reconciliation` (`down_revision=0063`).

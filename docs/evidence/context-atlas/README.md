# Context Atlas evidence

Allt här är genererat från den committade checkouten eller från publik GitHub-metadata.
Inga hemligheter, inga personliga sökvägar och ingen produktionsdata ingår.

| Fil | Innehåll | Reproduktion |
|---|---|---|
| `capability-map.json` | routers, modulpaket, modeller, migrationer, testreferenser, relationsluckor | `python backend/scripts/context_atlas.py map --repo-root . --out docs/evidence/context-atlas/capability-map.json` |
| `system-map.json` | 418 moduler, 220 endpoints, 21 eventmodeller, worker-hookar, maturity-tillstånd, lokal git-evidens | `... map --deep --with-git --out .../system-map.json` |
| `change-impact-trust.json` | direkta/transitiva beroenden, endpoints, tester, säkerhetsnoter för `app.rag.trust` | `... impact app.rag.trust --out ...` |
| `recommendation-memory.json` | läs-only rekommendation för "build a new memory engine" | `... recommend --capability memory --implies-failure --out ...` |
| `pull-requests.json` | normaliserad PR-metadata (#500–#504) fångad via GitHub API | `... prs /tmp/prs.json --out ...` |

## Körda kontroller

```
pytest tests/backend/test_context_atlas_*.py -q
-> 78 passed, 41 warnings in 174.67s
```

Testerna använder riktig Postgres, riktiga RLS-policyer och riktig content-addressed storage
för kandidatsökningen. De kör **inte** hela embedding-workern (kräver provideranrop).

```
export/verify av atlas-branchen (bas 8c7b335e)
-> 1 branch, isolerad klon, SHA + tree-hash + git fsck
```

Bundle-checksummor står i respektive `manifest.json`/`SHA256SUMS` i transferkatalogen
(`/tmp/context-atlas-transfer/`), som medvetet ligger utanför git.

## Vad artefakterna inte påstår

- `test_referenced` betyder att en testfil namnger komponenten — inte att beteendet täcks.
- `wired` betyder inkopplad i `main.py` eller importerad — inte att den fungerar i drift.
- `reviewed`/`integrated`/`activated`/`observed` är `null`: en checkout kan inte bevisa dem.
- `possible_supersession` är en kandidat, aldrig ett val av sanning; `canonical_truth` är alltid
  `null`.
- Varje artefakt har `execution_authorized: false`.

# Roadmap — LifeOS / MainAI

Fasindelad plan. Varje fas ska vara körbar och testbar innan nästa påbörjas.

## Fas 0 — Grund (denna leverans)
- [x] Systemarkitektur dokumenterad.
- [x] Projektstruktur (backend/frontend/docker).
- [x] PostgreSQL-schema för projekt, uppgifter, dokument, konversationer, providerkonfiguration.
- [x] Qdrant-anslutning och grundläggande collection-hantering.
- [x] Provider-abstraktion med stöd för OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter, lokal (Ollama).
- [x] Dokumentuppladdning → chunkning → embedding → indexering (RAG-pipeline, MVP).
- [x] Chat-endpoint med RAG-kontext och källhänvisningar.
- [x] Next.js-portal: Dashboard, Chat, Kunskapsdatabas, Projekt, Dokument, Admin (grundskal).
- [x] Docker Compose för hela stacken.
- [x] README med installations-/driftinstruktioner.

## Fas 1 — Stabil MVP i drift
- [x] Alembic-migrationer istället för `create_all` (se `backend/alembic/`, `docs/OPERATIONS.md`).
- [x] Autentisering (cookie-session, registrering, e-postverifiering) för portalen.
- [ ] Robust felhantering + loggning (strukturerad logg, request-ID).
- [ ] Enhetstester för provider-lager och RAG-pipeline.
- [ ] Kostnads-/tokenräkning per leverantör i adminpanelen.
- [ ] Verklig bakgrundskö för indexeringsjobb (t.ex. RQ/Celery) istället för synkron indexering.

## Fas 2 — Kunskapsdjup
- [ ] Webbplatscrawler: indexera hela den egna webbplatsen automatiskt, schemalagt.
- [ ] Källkodsindexering: koppla GitHub-repo, indexera filstruktur + kod som separat kunskapskälla.
- [ ] Automatiskt "minnesskrivning": AI:n föreslår vad som ska sparas permanent efter en konversation,
      användaren godkänner (ingen tyst datainsamling).
- [ ] Deduplicering och konfliktdetektering i kunskapsbiblioteket (undviker motstridig info).
- [ ] **Life Library / Life Knowledge Studio / LifeCast** — multimodal källimport (PDF, DOCX,
      bild, ljud, video, YouTube), Trust Engine-utökning (källa/tolkning/osäkerhet/konflikt
      per påstående) och genererade kunskapsprodukter + podcastproduktion. Planerad, ej
      påbörjad — se `docs/LIFE_LIBRARY_PLAN.md` för fullständig arkitektur och fasindelning
      (L0–L8). Byggspärr: kräver stabil Render-drift/kontoflöde och den bakgrundskö som redan
      står som öppen punkt i Fas 1 ovan.

## Fas 3 — Proaktiv AI
- [ ] Förslagsmotor: prioriteringar, nästa steg, riskflaggor baserat på roadmap + kunskapsbibliotek.
- [ ] Notifieringar i dashboard ("Founder AI säger").
- [ ] Uppgifts-/roadmap-hantering med AI-genererade delmål.

## Fas 4 — Lokal modell i produktion
- [ ] Utvärdera och driftsätta lokal modell (Llama/Qwen/Mistral) via Ollama som primär motor
      för rutinuppgifter, med molnleverantörer som fallback för tyngre resonemang.
- [ ] Finjustering/RAG-optimering mot kvalitetssäkrad projektdata.
- [ ] GPU-kapacitetsplanering för lokal inferens.

## Fas 5 — Skala & härdning
- [ ] Rollbaserad åtkomstkontroll (ägare/anställd/kund där relevant).
- [ ] Automatiserad backup + återställningstest (PostgreSQL + Qdrant).
- [ ] Observability: metrics, tracing, larm.
- [ ] Lastbalansering av backend, separat drift av Qdrant/Postgres vid behov.

## Uppskjutna produktområden (ej del av någon fas ovan)
- **Life Innovation & Patent Intelligence, ElevenLabs-forskning, multi-coin Life Compute &
  Mining / Life Mining Advisor** — långsiktig produktvision, konsoliderad men INTE
  påbörjad. Byggspärr: väntar på att MainAI:s grund är klar (se
  `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §8). Se
  `docs/LIFE_DEFERRED_PRODUCT_VISION_AND_VENDOR_PHILOSOPHY.md` för fullständig beskrivning
  och de principer (studera externa verktyg, lås inte in teknikval, anta inte lönsamhet) som
  styr dem.

## Icke-mål (medvetet uteslutet från MVP)
- Ingen SaaS-mellanhand för orkestrering (LangChain/LlamaIndex undviks initialt — egen enkel
  RAG-pipeline ger full kontroll och färre beroenden, i linje med kravet att undvika dyra
  mellanhänder).
- Ingen betaltjänst för vektordatabas — Qdrant körs självhostat i Docker.

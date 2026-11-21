# Founder Alpha functional completion v3

**Kodkandidat:** `59184c937947dece75a46e11ab600d2690c5a3c6`
**Bas:** `6ececab306d0c1551d6cefad3bb1c4b62fd8a8c9`
**Branch:** `claude/founder-alpha-fullstack-qa`
**Status:** funktionellt verifierad; F04, F23 och F24 är fortsatt BLOCKED.
**Ingen push, merge eller deployment har gjorts.** `first_cycle_go=false`.

## Vad som faktiskt ändrades

Denna omgång bygger vidare på `0db06d5`/`4814a1d` och lägger ingen ny arkitektur eller
migration ovanpå den befintliga. Den stänger två konkreta chattbrister:

1. **Durable claim utan svarsrad kunde ge permanent 409.**
   Om processen dog efter att konversation, användarmeddelande och `ChatRequestClaim`
   committats men innan `_claim_reply_attempt()` skapade assistentraden, behandlade
   `_replay_chat_request()` den saknade svarsraden som “pågår fortfarande”. Varje retry med
   samma `client_message_id` fastnade därför i 409. Replay går nu tillbaka in i exakt samma
   row-lockade försöksväg. Det är säkert eftersom originalprocessen bevisligen aldrig nådde
   provider-claimet; en samtidig duplicate kan bara förlora user-message-låset.

2. **Webbläsaren tappade idempotensnyckeln vid ett avbrutet svar.**
   `sendChat()` skapade en ny nyckel per anrop. Om servern hade sparat och besvarat men
   svaret aldrig nådde webbläsaren, kunde ett nytt försök skapa ett andra meddelande och ett
   andra provider-anrop. Chattsidan behåller nu samma `client_message_id` och erbjuder
   “Försök igen” för exakt samma logiska sändning. Vid omladdning visas ett avslutande
   användarmeddelande utan svarsrad som en återhämtningsbar assistentrad i stället för att
   tyst försvinna.

3. **Tangentbordsresan verifieras uttryckligen.** Chatten kan fokuseras, tabbas till
   Skicka-knappen och skicka med Enter.

Ingen produktionskod utanför chatten ändrades. Inga nya tabeller, migrationer, providers
eller parallella minnesvägar infördes.

## Verifierade resultat

| Kontroll | Resultat |
| --- | --- |
| Samlad backend-JUnit | **608 tester, 0 failures, 0 errors, 2 skips (606 passed)** |
| Chatt-sviten | **100 passed** |
| Minnes-/RAG-fokuserade sviter | **79 passed** |
| Bibliotek/chat/durable delivery | **182 passed, 1 skip** (`zip` saknas) |
| PR #464 fokustester, separat worktree | **66 passed, 0 failed, 0 skipped** |
| Playwright Founder Alpha | **13 expected, 0 unexpected, 0 flaky, 0 skipped** |
| Frontend production build | **PASS** |

De två backend-skipparna är fortsatt `zip`-binären och den opt-in live OpenAI-rundturen.
Playwright-körningen omfattar:

- full import → indexering → sökning → källciterat svar → öppna källa → fortsätta → radera;
- mediaimport, tidsstämplad transkription och källänk med `?t=`;
- mobil layout för `/library`, `/workbench` och media-detaljsidan;
- session efter reload, nätverksavbrott, projekt/uppgift och felmeddelanden;
- avbrutet chattsvar som återupptas med samma `user_message_id` och `assistant_message_id`;
- tangentbordsnavigering och Enter-sändning i chatten.

## Reproduktion

Backend-testerna kördes mot isolerad PostgreSQL/Redis:

```bash
cd backend
PYTHONPATH=. \
DATABASE_URL=postgresql://postgres@127.0.0.1:55432/lifeos_test_program \
APP_DATABASE_URL=postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test_program \
LIFEAI_TEST_DATABASE_NAME=lifeos_test_program LIFEAI_TEST_REDIS_DB=14 \
REDIS_URL=redis://127.0.0.1:56379/14 SECRET_KEY=local-test-secret COOKIE_SECURE=true \
pytest tests/backend/chat -q
```

Browserresan kördes mot en helt ny `lifeos_e2e_v3`, riktig backend/worker på `8010`, riktig
Next.js-proxy och isolerad Chromium 1194. Viktiga harness-krav:

```bash
export COOKIE_SECURE=true
export STORAGE_ROOT=/tmp/lifeai-testenv/e2e-v3-storage
export LD_LIBRARY_PATH=/tmp/lifeai-testenv/root/usr/lib/x86_64-linux-gnu:/tmp/lifeai-testenv/root/lib/x86_64-linux-gnu
export FONTCONFIG_PATH=/tmp/lifeai-testenv/root/etc/fonts
```

`STORAGE_ROOT` måste peka på en skrivbar isolerad katalog; standardvägen `/var/lib/lifeai`
är avsiktligt inte skrivbar för testanvändaren och gav tidigare ett harness-fel, inte en
produktbugg.

## Kvarstående verkliga blockerare

- **F04:** Ollama saknas/port `11434` svarar inte. Ingen verklig lokal modellkonversation kan
  verifieras. Tunga modeller ska inte installeras på VPS:en med 1,8 GiB RAM.
- **F23:** fyra timmars aktiv soak är inte körd. Runbook finns, men är inte bevis på körning.
- **F24:** QA-grinden ärver F04 och F23 och är därför BLOCKED, inte PASS.

## Kända begränsningar

- Chatten är fortsatt request/response, inte token-streaming. UI:n visar ett ärligt
  “MainAI skriver…”-tillstånd och levererar ett komplett svar; den påstår inte att den
  streamar. Token-streaming är en UX-förbättring, inte en påstådd funktion i denna kandidat.
- `pg_stat_statements` saknas.
- `zip` saknas för ett krypterat arkivtest.
- Z.ai-CLI-beroende tester och live OpenAI-rundturen körs inte utan respektive miljö.
- Exakt-head GitHub Actions kräver en godkänd push.
- CPU/minne/disk-utmattning är inte mätt.

## Evidensfiler

- `backend-tests.xml` — sammanslagen JUnit för 608 tester.
- `playwright-founder.json` — sammanslagen Playwright-JSON för 13 tester.
- `frontend-build.txt` — produktionsbygget för `59184c9`.
- `summary.json` — maskinläsbar sammanfattning av kandidat, resultat och blockerare.

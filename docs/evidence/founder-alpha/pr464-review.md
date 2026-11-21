# PR #464 — oberoende granskning

**PR:** https://github.com/d1n095/LifeAI/pull/464
**Titel:** `fix(integration): isolate R15 model evaluation and fence retry accounting`
**Status:** open/draft; ingen merge eller deployment.
**Head:** `86671468a6dd271b61a3f985434130c4bf470ea8`
**Bas:** `16a6bd301a8a389e7648b8a3c1cdd80a3d115662`
**CI synthetic merge:** `af07f15e94c3567027576d7f6483c69321e13c37`

## Metod

PR:en granskades som en faktisk diff mot basen, inte bara via PR-beskrivningen:

```bash
git diff --stat 16a6bd3..8667146
git worktree add --detach .review/pr464 8667146
```

I den separata worktreen kordes PR-headens egna R15- och chat-retrytester mot den
isolerade PostgreSQL/Redis-miljon:

```bash
PYTHONPATH=. /tmp/r15-venv/bin/python -m pytest \
  tests/backend/providers/test_r15_integration_security.py \
  tests/backend/chat/test_chat_message_persistence.py -q
```

**Resultat: 66 passed, 0 failed, 0 errors, 0 skipped** (78.69 s). Inga andringar
gjordes i PR #464 eller dess branch.

## Fynd

Ingen blockerande defekt kunde reproduceras i det granskade omradet. De viktigaste
skyddsmekanismerna som testerna faktiskt verifierar:

- **Retry och dubbelrakning:** en durabel reply-claim och row-locking serialiserar
  samtidiga forsok. En osaker provider-effect blockerar automatisk retry och fallback;
  endast en bevisad pre-effect-failure far slappas och forsokas igen. Retry av en redan
  lyckad reply ar idempotent och gor inget nytt provider-anrop.
- **Provider-timeout och 503:** en timeout klassificeras som
  `external_effect_unknown`, ar inte retrybar och lamnar en hall. En 503 for owerifierat
  externt pris avvisas fore provider-anropet. Misslyckade forsok rapporteras inte som
  lyckade.
- **Kostnad, token och jobbutfall:** usage- och reservation-idempotens ar bunden till
  owner/tenant/provider/model; en avvikande identitet avvisas i stallet for att
  dubbelrakna eller skriva over en annan post. Per-attempt-halls och atomisk
  reserve/finalize gor att en replay inte dubbelrakningar.
- **Modellutvarderingens isolering:** eval-providern ar en task-local ContextVar-scope.
  Den skriver inte global provider-registrering eller `provider_config`, faller inte
  tillbaka till en produktionsprovider och avvisar nestade scopes. En koperad context
  expirerar efter scope-exit; CLI-miljon ar en explicit allowlist utan HTTP-nycklar,
  databasuppgifter eller app-hemligheter.
- **Race conditions:** samtidiga reply-claims ger exakt en claim och en 409 for den
  andra; reservation/usage-finalize ar idempotent och identitetsbundet.

## Kvarstaende risker

- Hela PR:ens 3 114-test-CI och exakt-head GitHub Actions har inte korits om här;
  PR:en havdar PASS i sin egna CI, men det ar byggarens evidens, inte oberoende
  release-godkannande.
- Den riktiga `z-ai`-CLI:n och en riktig modell korits inte; 12 real-CLI/model-
  utvarderingar ar skippade i den befintliga CI-miljon.
- PostgreSQL och Redis delar inte en transaktion. Osakra halls kraver
  evidensbaserad avstamning, aldrig blind aterbetalning eller nytt anrop.
- Betald extern query-embedding ar fortsatt avstangd i guarded chat.
- Aldre MainAI-registry-anropare certifieras inte av dessa chat-regressioner.

## Slutsats

Ingen kodandring behovdes i `claude/founder-alpha-fullstack-qa` for detta omrade.
PR #464 lamnas orord och oforandrad; eventuella fynd ska hanteras av PR:ens agare.

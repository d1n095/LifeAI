# VPS Backup & Restore

Policy, verklig omfattning och en övningsprocedur för `scripts/vps/backup.sh` och
`scripts/vps/restore.sh`. Se `docs/VPS_ARCHITECTURE.md` för den fullständiga bilden av var
tillstånd faktiskt lever. Allt nedan är verifierat genom att faktiskt köra båda skripten mot
riktiga filer och riktiga Docker-volymer — se `.github/workflows/ci.yml`s
`vps-backup-restore-test`-jobb.

## Vad som faktiskt säkerhetskopieras

| Data | Var det lever | Täcks av `backup.sh`? |
|---|---|---|
| `docker-compose.vps.yml`, `Caddyfile` | `/opt/lifeai` | Ja |
| Deploy-poster (digests, tidsstämplar, resultat — se `scripts/vps/deploy.sh`) | `/opt/lifeai/deployments/*.json` | Ja |
| TLS-certifikat, ACME-kontostate | Docker-volymen `caddy_data` | Ja |
| Caddys egen autosparade config | Docker-volymen `caddy_config` | Ja |
| `/etc/lifeai/lifeai.env` (alla hemligheter) | `/etc/lifeai` | **Nej, avsiktligt** — se nedan |
| Postgres-databasen (all applikationsdata) | Supabase, INTE denna VPS | **Nej** — Supabases eget ansvar |
| Redis (rate limiting, sessioner) | Upstash, INTE denna VPS | **Nej** — förlust är ofarlig, se nedan |
| Docker-containrarnas egna loggar | `json-file`-loggdrivrutinen | **Nej** — redan rullande, inte katastrofrelevant |
| Uppladdade filer | Finns inte lokalt på VPS:en alls | N/A — se nedan |

### Varför hemligheter inte finns i arkivet

`backup.sh` skriver aldrig in `/etc/lifeai/lifeai.env` i arkivet. Ett okrypterat tar-arkiv på
samma disk som originalet är inget verkligt skydd för hemligheter — det multiplicerar bara var
en läcka kan komma ifrån, utan att faktiskt lösa "vad händer om disken förloras". I stället
skriver arkivet in `required_env_var_names` (bara NAMN, aldrig värden — samma lista
`scripts/vps/deploy.sh` redan validerar mot, delad via `scripts/vps/lib.sh`s
`$LIFEAI_REQUIRED_ENV_VARS`) i sin `manifest.json`, så en katastrofåterställning har en
checklista över vad som behöver återskapas från din egen säkra hemlighetslagring (t.ex. en
lösenordshanterare) — exakt samma steg som den allra första deployen
(`docs/STRATO_VPS_DEPLOY.md` Steg 2). Se `docs/VPS_SECRETS_INVENTORY.md` för den fullständiga
inventeringen (namn/kategorier, aldrig värden).

### Varför Postgres/Redis inte täcks

Databasen är Supabase-hostad, aldrig på den här VPS:en alls — ett VPS-lokalt backup-skript kan
inte ärligt hävda att det täcker en tjänst det aldrig rör vid. Supabase har sina egna
backup-mekanismer (point-in-time recovery m.m. beroende på plan); det är ett separat, medvetet
beslut som ligger utanför den här VPS-förberedelsens omfattning. Redis är Upstash-hostad och
innehåller enbart rate-limit-räknare och sessionsdata — att förlora den är ofarligt (räknare
nollställs, användare loggas ut, ingenting permanent går förlorat).

### Varför inga uppladdade filer

Appen sparar inget dokumentinnehåll som filer på disk — allt landar i Postgres
(`document_chunks`, se `docs/VPS_ARCHITECTURE.md` och `backend/app/rag/vector_store.py`).
Verifierat genom att gå igenom `backend/app/routers/*.py` efter filskrivningar utanför
test-only-verktyg — det finns inga.

## Köra en backup

```bash
cd /opt/lifeai
sudo ./scripts/vps/backup.sh
```

Skriver `/var/backups/lifeai/lifeai-backup-<TIDSSTÄMPEL>.tar.gz` (mode 0600) plus en
`.sha256`-checksummefil bredvid. Behåller de senaste 7 arkiven som standard (`--keep N` för
att ändra). Kräver INTE root — `/opt/lifeai` och `/var/backups/lifeai` ägs redan av
deploy-användaren (se `scripts/vps/30_setup_directories.sh`), och att läsa Docker-volymer
kräver bara den användarens docker-gruppmedlemskap.

**Rekommenderad frekvens:** innan varje `deploy.sh`-körning (fångar TLS-certifikatstate innan
en riskabel ändring) plus en daglig cron-rad om du vill ha kontinuerlig täckning — det finns
ingen automatisk schemaläggning inbyggd i skriptet självt (medvetet: att lägga till en
`cron`/`systemd timer`-rad är ett enda extra steg för Dennis att göra på den riktiga servern,
och att gissa fel schemaläggningsbeslut i kod som aldrig körs mot en riktig server är sämre än
att dokumentera det som ett manuellt steg här).

## Verifiera ett arkiv utan att återställa

```bash
sha256sum -c /var/backups/lifeai/lifeai-backup-<TIDSSTÄMPEL>.tar.gz.sha256
```

## Återställningsövning (kör detta periodiskt, inte bara vid en verklig incident)

En backup du aldrig har testat att återställa är inte en verifierad backup. Kör den här
övningen efter varje större ändring av `docker-compose.vps.yml`/`Caddyfile`, och annars minst
en gång per kvartal:

1. **Välj det senaste arkivet:**
   ```bash
   ARCHIVE=$(ls -t /var/backups/lifeai/lifeai-backup-*.tar.gz | head -n1)
   ```
2. **Återställ till ett engångskatalog** (skriptet vägrar av design att peka direkt på
   `/opt/lifeai` — se skriptets egen huvudkommentar):
   ```bash
   sudo ./scripts/vps/restore.sh --from "$ARCHIVE" --target-dir /tmp/restore-drill --confirm
   ```
3. **Inspektera resultatet:**
   - `diff /tmp/restore-drill/docker-compose.vps.yml /opt/lifeai/docker-compose.vps.yml`
     (ska vara identiska om inget har ändrats sedan backupen togs).
   - `docker volume ls | grep lifeai_restore_` — de nya, ISOLERADE volymerna med
     TLS-certifikat/Caddy-config.
   - `docker run --rm -v <lifeai_restore_...>:/vol:ro alpine ls -la /vol` för att bekräfta
     att certifikatfilerna faktiskt finns där.
4. **Städa upp övningen:**
   ```bash
   sudo rm -rf /tmp/restore-drill
   docker volume rm <lifeai_restore_...caddy_data> <lifeai_restore_...caddy_config>
   ```
5. **Dokumentera resultatet** (datum, vilket arkiv, om något var oväntat) — en enkel rad i
   din egen driftlogg räcker; det finns inget krav på ett specifikt format här.

Ingenting i steg 1–4 rör den körande stacken — `restore.sh` skapar bara nya, separat
namngivna volymer och en helt fristående katalog.

## Vid en verklig katastrof (disken/servern är helt förlorad)

1. Provisionera en ny VPS och kör `scripts/vps/00_preflight.sh` t.o.m.
   `scripts/vps/50_enable_auto_updates.sh` enligt `docs/STRATO_VPS_DEPLOY.md`.
2. Återställ det senaste arkivet till `/tmp/restore-drill` (steg 1–2 ovan), inspektera det, och
   kopiera sedan manuellt in `docker-compose.vps.yml`/`Caddyfile`/`deployments/` i det nya,
   riktiga `/opt/lifeai` — skriptet gör aldrig den sista kopieringen åt dig, se dess
   huvudkommentar.
3. Kör `sudo ./scripts/vps/restore.sh --from <arkiv> --target-dir /tmp/restore-drill
   --overwrite-live-volumes --confirm` **i stället** för steg 2 ovan om du vill återställa
   TLS-certifikaten direkt in i de riktiga `caddy_data`/`caddy_config`-volymerna (undviker att
   Let's Encrypt-hastighetsgränser triggas av en helt ny certifikatbegäran) — detta är den enda
   vägen som rör "levande" state, och kräver den extra flaggan uttryckligen.
2. Skapa `/etc/lifeai/lifeai.env` från grunden med hjälp av `manifest.json`s
   `required_env_var_names`-lista och din egen säkra hemlighetslagring — se
   `docs/STRATO_VPS_DEPLOY.md` Steg 2 och `docs/VPS_SECRETS_INVENTORY.md`.
3. Kör `sudo ./scripts/vps/deploy.sh --confirm` som vanligt.
4. Postgres/Redis behöver ingen åtgärd härifrån — de är redan externa och opåverkade av en
   VPS-diskförlust. Om Supabase/Upstash SJÄLVA behöver återställas, följ deras egna
   återställningsflöden (utanför den här dokumentets omfattning).

Se `docs/VPS_OPERATIONS_RUNBOOK.md` för den bredare incidenthanteringsprocessen.

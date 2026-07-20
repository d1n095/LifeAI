# VPS Docker-härdning

Vad som utvärderades, vad som faktiskt tillämpades, och varför — för `backend/Dockerfile`,
`frontend/Dockerfile` och `docker-compose.vps.yml`. Allt nedan är verifierat mot de RIKTIGA
images i `.github/workflows/ci.yml`s `vps-compose-verify`-jobb, inte bara skrivet i YAML och
antaget fungera. Se `docs/VPS_ARCHITECTURE.md` för den bredare bilden.

**Viktigt att veta:** `backend/Dockerfile`/`frontend/Dockerfile` används ENDAST av lokal
`docker-compose.yml` (utveckling) och denna VPS-topologi. Render kör `Dockerfile.combined`,
en helt separat fil — härdningen nedan rör alltså aldrig den redan verifierade
produktionslinjen på Render.

## Tillämpat

| Åtgärd | Var | Verifiering |
|---|---|---|
| Icke-root körningsanvändare (`appuser`/`nextjs`, UID 10001) | Båda Dockerfiles | `vps-compose-verify`: `docker inspect` mot `Config.User`/faktisk process-UID |
| Borttaget `build-essential`/`libpq-dev` från backend-imagen | `backend/Dockerfile` | Verifierat lokalt: hela `requirements.txt` installeras från förbyggda manylinux-hjul, ingen C-kompilator behövs alls (se commit-meddelandet) |
| `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1` | `backend/Dockerfile` | Loggar syns direkt i `docker logs` (redan synligt i alla tidigare CI-körningar av detta jobb) |
| `NEXT_TELEMETRY_DISABLED=1` | `frontend/Dockerfile` (bygg- och körsteg) | Inga utgående anrop till Vercels telemetri-tjänst från CI-byggen |
| OCI-etiketter (`org.opencontainers.image.*`) | Båda Dockerfiles | Läsbara via `docker inspect --format '{{json .Config.Labels}}'` |
| `init: true` (tini som PID 1) | Alla tre tjänster i `docker-compose.vps.yml` | Korrekt signalhantering/zombie-reaping oavsett vilken process som faktiskt blir PID 1 efter `exec` i entrypointen |
| `security_opt: [no-new-privileges:true]` | Alla tre tjänster | Ingen process i någon av dessa containrar behöver någonsin höja sina privilegier (t.ex. via setuid-binärer) |
| `cap_drop: [ALL]` | Alla tre tjänster | Ingen av processerna binder till en port < 1024, öppnar råa sockets, eller behöver någon annan Linux-capability — verifierat genom att appen fortsätter fungera fullt ut i `vps-compose-verify`s fullständiga begärandekedjetest |
| `read_only: true` + `tmpfs: [/tmp]` | Alla tre tjänster | Se "Vad varje container faktiskt skriver till" nedan — verifierat genom att hela testsviten (hälsokontroller, riktig HTTP-förfrågan genom hela kedjan) fortsätter fungera med skrivskyddat rotfilsystem |
| `pids_limit`, `mem_limit`, `cpus` | Alla tre tjänster | Konservativa startvärden för en liten VPS (se `docker-compose.vps.yml`s kommentar) — justera efter den riktiga serverns specifikationer, se `docs/STRATO_VPS_DEPLOY.md` |
| `request_body { max_size 30MB }` | `Caddyfile` | Ny CI-kontroll i `vps-compose-verify`: en förfrågan över gränsen avvisas av Caddy INNAN den når frontend/backend |
| `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Strict-Transport-Security` | `Caddyfile` | Verifierade lågrisk-headers som inte kräver käll-specifik justering (till skillnad från CSP, se nedan) |
| `HOSTNAME=0.0.0.0` för frontend | `docker-compose.vps.yml` | Fixar en verklig, tidigare oupptäckt bugg — se `docs/VPS_ARCHITECTURE.md` |

## Vad varje container faktiskt skriver till (grund för `read_only: true`)

- **Backend**: enda skrivningen vid körning är `scripts/ensure_app_role.py`s `mktemp()`-fil
  (som `docker-entrypoint.sh` sedan `source`:ar) — allt annat state ligger i Postgres, inte i
  containerns filsystem. Verifierat genom att grepa hela `backend/app/` och `backend/scripts/`
  efter `open(..., "w"/"a")`/`mkdir`/`tempfile`-användning utanför test-bara verktyg
  (`run_e2e_backend.py`, aldrig körd i produktion) och den lokala e-postutkorgs-fallbacken
  (`dev_mail_outbox_dir`, uttryckligen dokumenterad att aldrig sättas i produktion).
- **Frontend**: den fristående (`standalone`) Next.js-servern serverar förbyggd
  statisk/server-utdata och proxar `/api/*` — inget eget diskbaserat cacheläge eller
  uppladdningsmål i den här appen (uppladdningar går rakt igenom till backend). `tmpfs /tmp`
  är en säkerhetsmarginal, inte ett känt krav.
- **Caddy**: allt persistent state (TLS-certifikat, autosparad config) går redan till
  `caddy_data`/`caddy_config`, båda namngivna volymer som förblir skrivbara oavsett
  `read_only: true` på containerns rotfilsystem (volymer och tmpfs-monteringar är
  uttryckliga undantag från `read_only`, inte påverkade av den).

## Utvärderat men INTE tillämpat (och varför)

- **Content-Security-Policy**: kräver en verklig inventering av alla skript-/still-/anslut-
  källor appen faktiskt använder (Next.js egna chunkar, ev. inline-stilar från
  komponentbibliotek) — att gissa en CSP riskerar att blint bryta UI:t. Spårat som en
  uppföljningspunkt i `docs/VPS_THREAT_MODEL.md`, inte gissat här.
- **Mass-omskrivning av `.github/workflows/ci.yml`s befintliga ~13 jobb** (SHA-pinnade
  actions, etc.): se `docs/VPS_SUPPLY_CHAIN.md` — medvetet begränsat till en NY, isolerad
  supply-chain-kontroll snarare än att röra den redan verifierade produktionslinjens
  befintliga jobb under en autonom session, i linje med "Keep the existing verified
  production line intact".
- **Multi-stage-uppdelning av backend-imagen** (separat "builder"-steg): onödig efter att
  `build-essential`/`libpq-dev` togs bort helt — det finns inget kompilat att kassera mellan
  steg längre, en extra stage hade bara lagt till komplexitet utan vinst.

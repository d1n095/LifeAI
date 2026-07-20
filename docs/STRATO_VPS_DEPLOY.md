# Strato VPS-distribution

**Status 2026-07-20: förberedelse på egen branch (`claude/strato-vps-prep`), INGEN deploy
gjord, INGEN kontakt med någon riktig server ännu.** Det här dokumentet beskriver den exakta
installationsordningen för när Dennis är redo att göra det första, manuella klicket. Ingenting
här triggas automatiskt av CI eller av en merge.

## Varför en VPS istället för Render

Se `docs/RENDER_DEPLOY.md`s avsnitt **"ROTORSAK BEKRÄFTAD AV RENDER SUPPORT"** för hela
bakgrunden. Kort sammanfattat: Render support har bekräftat att `Dockerfile.combined`s
arkitektur — två webbservrar (FastAPI + Next.js) som syskonprocesser i EN Render Web Service —
gör att Render kan route:a publik trafik icke-deterministiskt till fel process, även när
Renders egen hälsokontroll visar `200 OK` kontinuerligt. Render rekommenderar själva separata
tjänster för det här mönstret. En VPS med riktiga, separata containrar bakom en riktig reverse
proxy (Caddy) är den arkitektur som faktiskt matchar den rekommendationen.

**Samma säkerhetsegenskap som `Dockerfile.combined` försökte uppnå med loopback-isolering
(`127.0.0.1:8000`, aldrig publicerad) återskapas här med riktig nätverksisolering** — backend
har ingen `ports:`-publicering alls i `docker-compose.vps.yml` och är bara nåbar från andra
containrar på det privata Docker-nätverket `lifeai_internal`, inte ens det direkt (bara
frontends egen proxy-route, `http://backend:8000`, någonsin anropar den).

## Arkitekturöversikt

```
Internet (443/80)
      │
      ▼
   ┌───────┐   automatisk HTTPS (Let's Encrypt), enda publika processen
   │ Caddy │
   └───┬───┘
       │  lifeai_internal (privat Docker-nätverk, ingen host-publicering)
       ▼
   ┌──────────┐   reverse_proxy frontend:3000
   │ frontend │   (Next.js — samma image som Render, se frontend/Dockerfile)
   └────┬─────┘
        │  INTERNAL_API_URL=http://backend:8000, server-till-server, Docker-DNS
        ▼
   ┌─────────┐   ingen ports:-publicering — bara nåbar via lifeai_internal
   │ backend │   (FastAPI — samma image som Render, se backend/Dockerfile)
   └────┬────┘
        │  publik internet (utanför VPS:en)
        ▼
  Supabase Session Pooler (Postgres)   Upstash (Redis)
```

Postgres och Redis körs INTE på VPS:en — samma externa Supabase Free (Session pooler, port
5432) och Upstash Free som redan används för Render, se `docs/RENDER_DEPLOY.md`s
"Databasrollerna"-avsnitt. VPS:en har därmed inget eget stateful data förutom hemlighetsfilen
och Caddys egna TLS-certifikat (se "Backup och rollback" nedan).

Backend- och frontend-images byggs INTE på VPS:en. `.github/workflows/build-images.yml` bygger
och pushar dem till GHCR (`ghcr.io/d1n095/lifeai-backend`, `ghcr.io/d1n095/lifeai-frontend`),
digest-pinnade. VPS:en gör bara `docker compose pull` mot en digest Dennis själv väljer och
skriver in — se "Första manuella deploy" nedan.

## Förutsättningar

- En Strato VPS med Ubuntu 24.04 LTS, root-/sudo-åtkomst via SSH.
- En domän (eller subdomän) vars DNS A-post pekar på VPS:ens publika IPv4-adress — krävs för
  att Caddy ska kunna slutföra Let's Encrypts HTTP-01-utmaning på port 80/443.
- Samma Supabase- och Upstash-uppgifter som redan finns för Render (inga nya konton behövs).
- En GitHub Personal Access Token med enbart `read:packages`-scope, för att VPS:en ska kunna
  `docker login ghcr.io` och pulla privata images (skapas separat från alla andra hemligheter,
  se Steg 3 nedan).

## Exakt installationsordning

### Steg 1 — Kör bootstrap-skripten (`scripts/vps/`)

Alla manuella `apt`/`useradd`/`ufw`-kommandon som tidigare stod utskrivna här har ersatts av
idempotenta, `--dry-run`-stödda skript i `scripts/vps/` — se `scripts/vps/README.md` för hela
listan och `docs/VPS_BOOTSTRAP.md` för säkerhetsresonemanget bakom varje skript (särskilt
`41_harden_ssh.sh`, som VÄGRAR stänga av lösenordsinloggning om den inte kan verifiera att en
riktig publik nyckel redan finns installerad). Att ha EN källa till sanning (skripten) istället
för både utskrivna kommandon här OCH separata skript förhindrar att de två divergerar över tid.

Körs som `root` (via `sudo`), i denna ordning, från en klon av repot på servern (eller kopiera
bara `scripts/vps/`-katalogen dit om du inte vill klona hela repot ännu):

```bash
sudo ./scripts/vps/00_preflight.sh --domain <din-domän>   # skrivskyddad, säker att köra om när som helst
sudo ./scripts/vps/10_install_docker.sh
sudo ./scripts/vps/20_create_deploy_user.sh --user dennis
# --- nu, från DIN EGEN maskin: ---
ssh-copy-id dennis@<vps-ip>
# Verifiera att `ssh dennis@<vps-ip>` loggar in UTAN lösenord innan du fortsätter.
sudo ./scripts/vps/30_setup_directories.sh --user dennis
sudo ./scripts/vps/40_configure_firewall.sh
sudo ./scripts/vps/50_enable_auto_updates.sh
# --- valfritt, ENDAST efter att lösenordsfri inloggning är verifierad ovan: ---
sudo ./scripts/vps/41_harden_ssh.sh --user dennis
sudo ./scripts/vps/90_verify_installation.sh --user dennis
```

Kör varje steg först med `--dry-run` om du vill se exakt vad det skulle göra innan det
faktiskt gör det. `90_verify_installation.sh` avslutar med en tydlig "redo för Steg 2"-signal
eller en lista över vad som fortfarande saknas.

`/opt/lifeai` får en gles git-checkout av repot (bara det som faktiskt behövs på servern —
`docker-compose.vps.yml`, `docker-compose.vps.ci.yml` behövs INTE här, `Caddyfile`):

```bash
cd /opt/lifeai
git clone --depth 1 --branch main --filter=blob:none --sparse https://github.com/d1n095/LifeAI.git .
git sparse-checkout set docker-compose.vps.yml Caddyfile
```

Ingen `backend/` eller `frontend/` källkod hämtas hit — VPS:en bygger aldrig produktion
lokalt, den pullar bara färdiga images (se "Varför" i inledningen).

### Steg 2 — Hemligheter (`/etc/lifeai/lifeai.env`)

**Aldrig i Git.** `.env.vps.example` i repot är mallen — kopiera den till servern (inte via
`git clone`, som redan är sparse och saknar den avsiktligt) och fyll i på servern direkt:

```bash
# Från din egen maskin, en gång:
scp .env.vps.example dennis@<vps-ip>:/tmp/lifeai.env.template

# På servern:
sudo mv /tmp/lifeai.env.template /etc/lifeai/lifeai.env
sudo chown root:root /etc/lifeai/lifeai.env
sudo chmod 600 /etc/lifeai/lifeai.env
sudo nano /etc/lifeai/lifeai.env   # fyll i varje värde, se .env.vps.example för vad varje rad styr
```

Generera `SECRET_KEY` och `MAINAI_APP_PASSWORD` med `openssl rand -hex 32` var — **egna,
oberoende värden, återanvänd aldrig Renders**, så en eventuell kompromettering av det ena
systemet aldrig automatiskt ger tillgång till det andra.

`BACKEND_IMAGE`/`FRONTEND_IMAGE` lämnas tomma här — de fylls i i Steg 5, det manuella
deploy-steget, aldrig innan.

### Steg 3 — Logga in mot GHCR på VPS:en

```bash
# PAT med ENBART read:packages, skapad separat från alla andra hemligheter.
echo "<GHCR_READ_PACKAGES_PAT>" | sudo docker login ghcr.io -u <github-användarnamn> --password-stdin
```

`docker login` sparar autentiseringen i root's `~/.docker/config.json` — konsekvent med att
`docker compose` för produktion alltid körs som root/sudo (Steg 5), aldrig som `dennis`
direkt, exakt som `/etc/lifeai/lifeai.env` bara är läsbar för root.

### Steg 4 — GitHub Actions bygger och pushar images

Redan klart som kod (`.github/workflows/build-images.yml`) — körs automatiskt vid push till
`main`, eller manuellt via `workflow_dispatch`. Bygger `backend/Dockerfile` och
`frontend/Dockerfile` (samma Dockerfiles som redan används för Render — inga separata
VPS-specifika Dockerfiles behövs) och pushar till GHCR, taggade med commit-SHA. Skriver ut den
digest-pinnade referensen (`ghcr.io/d1n095/lifeai-backend@sha256:...`) i jobbets Summary —
det är den strängen som klistras in i `/etc/lifeai/lifeai.env` i nästa steg, aldrig en flyktig
tagg som `:latest`.

**Detta jobb deployar ingenting och rör aldrig VPS:en.** Det fyller bara registret.

### Steg 5 — Första manuella deploy (kräver Dennis uttryckliga godkännande)

**Görs bara efter att Dennis själv, uttryckligen, pekat ut vilken digest som ska köras.** Inget
i det här repot eller i CI SSH:ar in på servern eller kör detta åt honom.

```bash
# 1. Hämta digesten från GitHub Actions-jobbets Summary (Steg 4) och skriv in den i
#    /etc/lifeai/lifeai.env:
sudo nano /etc/lifeai/lifeai.env
#   BACKEND_IMAGE=ghcr.io/d1n095/lifeai-backend@sha256:<den faktiska digesten>
#   FRONTEND_IMAGE=ghcr.io/d1n095/lifeai-frontend@sha256:<den faktiska digesten>

# 2. Pulla exakt de images'.
cd /opt/lifeai
sudo docker compose -f docker-compose.vps.yml pull

# 3. Starta.
sudo docker compose -f docker-compose.vps.yml up -d

# 4. Verifiera (se "Verifiering efter deploy" nedan) INNAN du lämnar terminalen.
```

En framtida omdeploy (ny digest efter en kodändring) upprepar bara steg 1-4 ovan — aldrig
`git pull` av källkod på servern, aldrig en lokal `docker build` här.

### Steg 6 — Health checks och restart-policies

Redan inbyggda i `docker-compose.vps.yml`: `restart: unless-stopped` på alla tre tjänster,
`healthcheck:` på backend (`curl /api/health`) och frontend (`node -e ...` mot samma
route via proxyn), och `depends_on: condition: service_healthy` så frontend väntar in en
frisk backend och Caddy väntar in en frisk frontend innan den börjar route:a trafik dit —
samma typ av startup-ordningsgaranti som `scripts/entrypoint-combined.sh` gav inuti EN
container, nu uttryckt som riktiga Compose-beroenden mellan containrar istället.

### Steg 7 — Loggrotation

`docker-compose.vps.yml` sätter `logging: driver: json-file, max-size: 10m, max-file: 5` på
alla tre tjänster — Dockers egen inbyggda rotation, ingen extra host-level `logrotate`-config
behövs för containerloggarna. Caddys egen accessloggfil (`/data/access.log` inuti containern,
se `Caddyfile`) roterar separat via Caddys `roll_size`/`roll_keep`-direktiv (10 MB × 5 filer) —
den ligger i volymen `caddy_data`, se backup-avsnittet.

### Steg 8 — Brandvägg (upprepat härifrån för fullständighetens skull)

Redan satt i Steg 1 (`scripts/vps/40_configure_firewall.sh`): `ufw` tillåter bara SSH/80/443 inkommande, allt annat nekas. Docker
manipulerar normalt sina egna `iptables`-regler direkt och kan i vissa konfigurationer
kringgå `ufw` för publicerade portar — eftersom bara Caddy publicerar något alls (`ports:` i
`docker-compose.vps.yml`), och det redan är 80/443 som `ufw` explicit tillåter, finns ingen
öppning det här faktiskt skulle exponera. Backend/frontend har ingen `ports:`-rad att
exponeras via över huvud taget.

### Steg 9 — SSH-härdning (upprepat härifrån)

Redan satt i Steg 1 (`scripts/vps/41_harden_ssh.sh`, om du valde att köra det): enbart
nyckelbaserad auth, `PermitRootLogin no`. Rotera VPS:ens SSH-värdnycklar
om du någonsin misstänker att den privata nyckeln på din egen maskin exponerats.

## Backup- och rollback-plan

**Vad som faktiskt behöver säkerhetskopieras — bara `/etc/lifeai/lifeai.env` och Caddys
`caddy_data`-volym (TLS-certifikat, av/på-läge går att återskapa gratis från Let's Encrypt om
den förloras, så det är bekvämlighet, inte kritiskt).** Postgres och Redis är externa
(Supabase/Upstash) och har sina egna backup-rutiner utanför den här VPS:en helt.

```bash
# Backup (kör t.ex. som ett dagligt cron-jobb som root):
sudo tar czf /root/lifeai-backup-$(date +%F).tar.gz \
  /etc/lifeai/lifeai.env \
  -C /var/lib/docker/volumes/lifeai_caddy_data/_data .
# Flytta backupfilen off-box (t.ex. till en separat lagringstjänst) — en backup som bara
# ligger kvar på samma VPS överlever inte om VPS:en själv går förlorad.
```

**Rollback vid en trasig deploy:**

```bash
# 1. Sätt tillbaka föregående kända goda digest i /etc/lifeai/lifeai.env (håll en logg över
#    vilken digest som kördes senast, t.ex. i en enkel textfil bredvid — inte i Git).
sudo nano /etc/lifeai/lifeai.env

# 2. Pulla och starta om exakt som i Steg 5.
cd /opt/lifeai
sudo docker compose -f docker-compose.vps.yml pull
sudo docker compose -f docker-compose.vps.yml up -d

# 3. Verifiera enligt nästa avsnitt.
```

Eftersom varje deploy pinnas till en specifik, oföränderlig digest (aldrig `:latest`) är
"föregående digest" alltid känd och exakt — ingen gissning om vilken kodversion som faktiskt
körde innan.

## Verifiering efter deploy

1. `https://<din-domän>/api/health` → `{"status":"ok"}` (går genom hela kedjan: Caddy →
   frontend → backend → Supabase/Upstash).
2. `sudo docker compose -f /opt/lifeai/docker-compose.vps.yml ps` → alla tre tjänster
   `running (healthy)`.
3. `sudo docker port <backend-container>` och `sudo docker port <frontend-container>` → tomt
   (ingen host-publicering) — samma kontroll som `vps-compose-verify`-CI-jobbet gör
   automatiskt mot samma `docker-compose.vps.yml`, se `.github/workflows/ci.yml`.
4. `sudo docker compose -f /opt/lifeai/docker-compose.vps.yml logs --tail=50` → inga
   återkommande omstarter, inga krascher.

## Vad CI redan bevisar innan detta någonsin körs på riktigt

`.github/workflows/ci.yml`s `vps-compose-verify`-jobb (branch-gated på
`claude/strato-vps-prep`) bygger de RIKTIGA `backend/Dockerfile`/`frontend/Dockerfile`-imagen
och kör den RIKTIGA `docker-compose.vps.yml` (plus en CI-bara `docker-compose.vps.ci.yml`-
overlay som bara lägger till `host.docker.internal`-åtkomst till en tillfällig Postgres/Redis
på GitHub Actions-runnern — se den filens kommentarer) och verifierar:

- Caddy är den enda tjänsten som publicerar något till host.
- Backend är overksamt onåbar utifrån (även via containerns eget nätverks-IP, inte bara host).
- `restart: unless-stopped` och begränsad `json-file`-loggning är faktiskt satt på alla tre
  containrar, inte bara skrivet i YAML utan att verkligen tillämpas.
- Ett riktigt HTTP-anrop genom hela kedjan (Caddy → frontend → backend → Postgres) svarar
  korrekt.

Detta är samma bevisdrivna metod som `combined-container-verify` redan använde för
`Dockerfile.combined` — verifiera mot den riktiga artefakten i CI, inte bara läsa YAML och anta
att den gör vad kommentarerna säger.

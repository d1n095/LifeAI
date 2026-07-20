# Strato VPS — driftsguide (runbook)

Vardagsdrift och incidenthantering för den riktiga servern, en gång den finns. Se
`docs/VPS_ARCHITECTURE.md` för hur systemet hänger ihop och `docs/VPS_THREAT_MODEL.md` för
vad det skyddas mot. Varje avsnitt nedan är skrivet mot ett skript/kommando som faktiskt
finns i det här repot — inga hypotetiska verktyg.

## Snabbkommandon

```bash
# Status för alla tre tjänster
sudo docker compose --env-file /etc/lifeai/lifeai.env -f /opt/lifeai/docker-compose.vps.yml ps

# Loggar (senaste 100 rader, alla tjänster)
sudo docker compose --env-file /etc/lifeai/lifeai.env -f /opt/lifeai/docker-compose.vps.yml logs --tail=100

# Hälsokontroll genom hela kedjan
curl -sS https://<din-domän>/api/health

# Deployhistorik (senaste 10)
ls -t /opt/lifeai/deployments/*.json | head -10 | xargs -I{} sh -c 'echo "--- {} ---"; cat {}'
```

## Rutindrift

### Innan varje deploy
```bash
cd /opt/lifeai
sudo ./scripts/vps/backup.sh          # se docs/VPS_BACKUP_RESTORE.md
sudo ./scripts/vps/deploy.sh --confirm
```

### Efter varje deploy
Följ "Verifiering efter deploy" i `docs/STRATO_VPS_DEPLOY.md` — `deploy.sh` gör redan detta
automatiskt (inklusive automatisk rollback vid misslyckande), men en manuell dubbelkoll costar
inget.

### Periodiskt (rekommenderat: månadsvis)
- Kör en återställningsövning enligt `docs/VPS_BACKUP_RESTORE.md`.
- Kör `sudo ./scripts/vps/90_verify_installation.sh --user lifeai` för att bekräfta att
  bootstrap-tillståndet (brandvägg, auto-updates, katalogbehörigheter) fortfarande är intakt.
- Granska `docker system df` för att se om gamla, oanvända images/volymer byggts upp
  (`docker image prune` — ALDRIG `docker system prune -a --volumes` blint, det kan radera
  `caddy_data`/`caddy_config` om de för tillfället inte är i bruk).

## Incidenter

### En färsk deploy misslyckades och rullade tillbaka automatiskt

Normalt beteende, inte en krissituation i sig — `deploy.sh` gjorde exakt vad det ska.
1. Läs den FELANDE deploy-postens fil (loggas av `deploy.sh` som "Failed record: ...").
2. `sudo docker compose --env-file /etc/lifeai/lifeai.env -f /opt/lifeai/docker-compose.vps.yml logs backend frontend caddy` — hitta den faktiska felorsaken (krasch vid start? hälsokontroll som aldrig blir grön? fel migration?).
3. Fixa grundorsaken i koden, låt CI (`vps-compose-verify`) bevisa fixen innan nästa
   `deploy.sh`-försök.

### Automatisk rollback MISSLYCKADES också (`deploy.sh` avslutade med kod 2)

Detta är den akuta situationen — tjänsten kan vara nere.
1. **Kontrollera faktiskt liv:** `curl -sS https://<domän>/api/health` — om den redan svarar
   `{"status":"ok"}` trots felmeddelandet, var det troligen en flaky hälsokontroll under
   tidspress (öka `--timeout` nästa gång), inte ett verkligt driftstopp.
2. Om tjänsten faktiskt är nere: kör `sudo ./scripts/vps/rollback.sh --confirm` manuellt igen
   — `deploy.sh`s automatiska anrop kan ha misslyckats av ett övergående skäl (t.ex. en
   tillfällig nätverksblip mot GHCR under `compose pull`).
3. Om `rollback.sh` säger "No previous successful deployment record found": det finns ingen
   känd god digest att gå tillbaka till (t.ex. första deployen någonsin gick fel). Gå till
   `/opt/lifeai/deployments/` manuellt, hitta den senaste posten med `"result": "success"` om
   någon finns; om ingen finns, deploya en tidigare, känt fungerande image-digest manuellt via
   `sudo docker compose --env-file /etc/lifeai/lifeai.env -f docker-compose.vps.yml pull && ... up -d`
   efter att ha satt `BACKEND_IMAGE`/`FRONTEND_IMAGE` i `/etc/lifeai/lifeai.env` till en digest
   du vet fungerade (från GHCR:s eget taggnings-/versionshistorik).
4. Om `rollback.sh` körde men tjänsterna ändå inte blev friska ("Rollback completed but
   services did not become healthy"): felet är troligen INTE i applikationskoden (samma
   digest som redan kördes framgångsrikt innan) utan i något som ändrats UTANFÖR koden sedan
   dess — kontrollera: Supabase/Upstash-tillgänglighet (`docs/VPS_ARCHITECTURE.md`s externa
   beroenden), diskutrymme (`df -h`), och om `/etc/lifeai/lifeai.env` av misstag redigerats.
5. Om ingenting ovan löser det: detta är en verklig P1. Dokumentera exakt vad som prövats
   (för framtida uppdatering av denna runbook) och eskalera till Dennis direkt — det finns
   ingen ytterligare automatiserad återhämtningsväg i det här repot.

### Väntande omstart efter en kärnuppdatering

`scripts/vps/50_enable_auto_updates.sh` installerar säkerhetsuppdateringar automatiskt men
startar ALDRIG om servern automatiskt (en medveten, separat avvägning — se det skriptets egen
kommentar). Kontrollera och hantera manuellt:

```bash
# Finns en väntande omstart?
[ -f /var/run/reboot-required ] && echo "Omstart krävs" && cat /var/run/reboot-required

# Om ja: schemalägg den för en period med förväntat lägst trafik (den här appen har en enda
# grundare-användare, så "lägst trafik" är sannolikt "när Dennis inte aktivt använder den").
# Tjänsterna har restart: unless-stopped (docker-compose.vps.yml) så de startar om
# automatiskt efter omstarten UTAN manuellt ingripande — men kör ändå en hälsokontroll direkt
# efteråt:
sudo reboot
# --- vänta, återanslut via SSH ---
curl -sS https://<domän>/api/health
sudo docker compose --env-file /etc/lifeai/lifeai.env -f /opt/lifeai/docker-compose.vps.yml ps
```

### Misstänkt läckt/stulen SSH-nyckel för deploy-användaren

1. Ta omedelbart bort den misstänkta nyckeln från `/home/lifeai/.ssh/authorized_keys` (eller
   motsvarande användarnamn).
2. Generera ett nytt nyckelpar på din egen maskin, lägg till den NYA publika nyckeln i
   `authorized_keys` (ALDRIG via det borttagna/misstänkta paret).
3. Verifiera att den nya nyckeln faktiskt fungerar för inloggning INNAN du stänger den
   nuvarande SSH-sessionen (samma försiktighetsprincip som `41_harden_ssh.sh` redan kräver
   för det första passwordless-login-testet).
4. Granska `sudo docker compose ... logs` och `/opt/lifeai/deployments/` efter tidsstämplar
   för att se om något deployats utan att Dennis gjorde det.
5. Rotera `SECRET_KEY` i `/etc/lifeai/lifeai.env` om obehörig åtkomst bekräftas (detta
   ogiltigförklarar ALLA befintliga sessioner — se `docs/AUTH_THREAT_MODEL.md`) och kör
   `deploy.sh` igen för att ladda om den.

### Disken tar slut

```bash
df -h /
sudo docker system df                 # vad tar faktiskt utrymme
sudo docker image prune               # ta bort oanvända images (INTE -a --volumes)
ls -la /var/backups/lifeai/           # gamla backup.sh-arkiv — backup.sh prunar redan
                                       # automatiskt (--keep, default 7), men kontrollera om
                                       # den inte körts på ett tag
```

### Certifikatförnyelse misslyckas / TLS slutar fungera

Caddy hanterar Let's Encrypt-förnyelse helt automatiskt så länge port 80/443 är nåbara och
DNS pekar rätt — de vanligaste verkliga orsakerna till att detta går fel:
1. DNS pekar inte längre på rätt IP (`dig <domän>` för att kontrollera).
2. `ufw` blockerar av misstag 80/443 (`sudo ufw status` — ska visa dem som ALLOW).
3. `caddy_data`-volymen skadad/förlorad → se `docs/VPS_BACKUP_RESTORE.md`s
   återställningsprocedur, eller låt Caddy begära ett helt nytt certifikat (fungerar också,
   bara långsammare och begränsat av Let's Encrypts hastighetsgränser).

## Loggplatser

| Vad | Var |
|---|---|
| Applikationsloggar (backend/frontend/caddy) | `docker compose logs` (json-file-drivrutin, roterande, se `docker-compose.vps.yml`) |
| Caddys egen åtkomstlogg | `/data/access.log` inuti `caddy_data`-volymen (roterande, se `Caddyfile`) |
| Deployhistorik | `/opt/lifeai/deployments/*.json` |
| Systemloggar (SSH, ufw, unattended-upgrades) | `journalctl`, `/var/log/ufw.log`, `/var/log/unattended-upgrades/` |

## Eskalering

Det finns ingen automatiserad eskaleringskedja för det här enanvändarsystemet — Dennis är
den enda operatören. Om en incident inte kan lösas med proceduren ovan: dokumentera exakt vad
som prövats och vad som fortfarande felar, så nästa felsökningspass (mänskligt eller
AI-assisterat) inte behöver börja om från noll.

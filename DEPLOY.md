# Deploy Aura online (altid tændt)

Målet: Aura kører 24/7 på en server, så din PC ikke er involveret. Vi bruger **Railway**
(nemmest, ~$5/md). Du får en fast HTTPS-adresse, som Telegram peger permanent på.

## Forudsætning
- En **GitHub-konto** (gratis) — koden lægges der, og Railway henter den derfra.
- Filerne `Procfile`, `Dockerfile`, `requirements.txt` ligger allerede klar i mappen.

## Trin 1 — læg koden på GitHub
I `aura`-mappen:
```cmd
git init
git add .
git commit -m "Aura"
```
Opret et tomt repo på github.com (privat!), og kør de to linjer GitHub viser dig, fx:
```cmd
git remote add origin https://github.com/DIT-NAVN/aura.git
git branch -M main
git push -u origin main
```
> `.env` og `aura.db` ryger IKKE med (de står i `.gitignore`) — nøgler holdes hemmelige.

## Trin 2 — opret projekt på Railway
1. Gå til railway.app → log ind med GitHub.
2. **New Project → Deploy from GitHub repo** → vælg `aura`.
3. Railway bygger automatisk (den ser `Dockerfile`/`Procfile`).

## Trin 3 — sæt miljøvariabler
I Railway-projektet → **Variables** → tilføj (samme som din `.env`):
```
TELEGRAM_TOKEN, OPENAI_API_KEY, OPENAI_MODEL, ORDRESTYRING_KEY,
ORDRESTYRING_BASE, LEADER_GROUP_CHAT_ID, WEBHOOK_SECRET, TZ
```

## Trin 4 — find din offentlige adresse
Railway → **Settings → Networking → Generate Domain**. Du får fx:
`https://aura-production.up.railway.app`

## Trin 5 — peg Telegram permanent derhen
Indsæt i browseren (udskift token + domæne + secret):
```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://DIT-DOMÆNE/telegram/<WEBHOOK_SECRET>
```
Svar: `{"ok":true,...}`. Nu kører Aura online — luk roligt din PC.

## Trin 6 — opret brugerne på serveren
Brugerne lå i en lokal `aura.db`. På serveren er databasen tom til at starte med.
Kør i Railway → projektets **Shell/Console** (eller via en engangs-deploy-kommando):
```
python manage.py adduser <telegram_id> <navn> <pro|jun>
```
(Alternativt: skift senere til en delt database som Railway Postgres, så data overlever genstarter.)

## Skifte tilbage til Make (hvis nødvendigt)
Sæt blot webhooken tilbage til Make-URL'en med samme `setWebhook`-kald.

---
**Andre nemme værter:** Render (samme GitHub-flow), Fly.io (`fly launch` fra mappen, ingen GitHub),
eller en lille VPS (Hetzner ~$5/md) med `uvicorn` bag nginx.

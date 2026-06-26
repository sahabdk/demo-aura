# Aura — Python-version (FastAPI)

Produktions-/billig-version af Aura. Erstatter Make.com med en lille Python-tjeneste, så du
betaler en **fast** serverpris i stedet for pr. operation. Spejler det vi byggede i Make:
Telegram-assistent (tekst + tale), ordrestyring-integration, roller (pro/jun), aftaler,
eskalerende rykkere og planlagte påmindelser.

## Arkitektur
```
Telegram ──webhook──▶ FastAPI (app/main.py)
                         │
                         ├─ auth + rolle (app/db.py, SQLite)
                         ├─ tale→tekst (Whisper, app/telegram.py)
                         ├─ agent = OpenAI function-calling (app/agent.py)
                         │     └─ værktøjer (app/tools.py) ─▶ ordrestyring (app/ordrestyring.py)
                         └─ svar tilbage til Telegram (tekst)
APScheduler (app/scheduler.py): morgen-oversigt, "om lidt"-påmindelser, ugentlig faktura-oversigt
```

| Fil | Ansvar |
|---|---|
| `app/main.py` | FastAPI, webhook, fejl-notifikation til leder ("safety net") |
| `app/agent.py` | Agent-loop + system prompt |
| `app/tools.py` | Værktøjer + skemaer + rolle-tilladelser |
| `app/ordrestyring.py` | ordrestyring API-klient (alle quirks indbygget) |
| `app/db.py` | Brugere/roller, rykker-tæller, aftaler, samtale-hukommelse |
| `app/email.py` | Eskalerende betalingspåmindelser (1./2./3.) |
| `app/scheduler.py` | Planlagte opgaver |
| `manage.py` | CLI til brugere/roller |

## Kom i gang
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # udfyld nøgler
python manage.py init
python manage.py adduser 7713099063 Dan pro
python manage.py adduser 6779276258 Sahab jun
uvicorn app.main:app --reload --port 8000
```

## Forbind Telegram-webhooken
Eksponér porten (fx via en server med HTTPS, eller `ngrok http 8000` til test), og sæt webhooken:
```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://DIT-DOMÆNE/telegram/<WEBHOOK_SECRET>"
```

## Deploy (vælg én — alle billige)
- **Railway / Render / Fly.io**: peg på repoet, sæt env-vars, deploy. ~$5–7/md.
- **VPS (Hetzner/DigitalOcean ~$5/md)**: `uvicorn`/`gunicorn` bag nginx + systemd.

Start-kommando i produktion:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Hvad der bevidst er holdt simpelt (udvid efter behov)
- **Aftaler** ligger i SQLite (ikke Google Calendar) — enklere, ingen OAuth. Vil du have rigtige
  Google-kalendere pr. medarbejder, kan `husk_aftale`/`se_aftaler` i `app/tools.py` pege på
  Google Calendar API i stedet.
- **Email** sender via SMTP hvis `SMTP_*` er sat; ellers "dry-run" (logger).
- **Færdigmelding** (`close_case`) sætter `work_done`; tilføj firmaets "afsluttet"-status-id
  fra `/case-statuses` når det skal lukkes helt.
- **CVR/feltnavne** og alle ordrestyring-detaljer er dokumenteret i projektets hoveddokument.

## Omkostning vs. Make
| | Make | Python her |
|---|---|---|
| Orkestrering | pr. operation (dyrt ved 150 ordrer/dag) | fast ~$5–12/md hosting |
| OpenAI / ordrestyring / Telegram | uændret | uændret |

Ved jeres volumen halverer Python ca. den samlede driftspris.

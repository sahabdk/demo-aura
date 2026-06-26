"""FastAPI-indgang: modtager Telegram-webhooks, kører agenten, svarer."""
import logging
from fastapi import FastAPI, Request, HTTPException

from .config import WEBHOOK_SECRET, LEADER_GROUP_CHAT_ID
from . import db, telegram
from .agent import run_agent
from .scheduler import start_scheduler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aura")

app = FastAPI(title="Aura")


@app.on_event("startup")
def _startup():
    db.init_db()
    db.seed_users_from_env()   # opretter brugere fra SEED_USERS-miljøvariablen
    start_scheduler()
    log.info("Aura startet")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(403, "forkert webhook-token")

    update = await request.json()
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    chat = msg.get("chat", {})
    if chat.get("type") != "private":   # Aura svarer kun i privat chat
        return {"ok": True}

    from_id = str(msg["from"]["id"])
    user = db.get_user(from_id)
    if not user:
        telegram.send_message(chat["id"], "Du har ikke adgang til Aura. Kontakt din leder.")
        return {"ok": True}

    # Tekst eller talebesked
    if msg.get("text"):
        text = msg["text"]
    elif msg.get("voice"):
        try:
            text = telegram.transcribe_voice(msg["voice"]["file_id"])
        except Exception as e:
            log.exception("whisper-fejl")
            telegram.send_message(chat["id"], "Jeg kunne ikke forstå talebeskeden — prøv igen.")
            _notify_leader(f"Whisper-fejl: {e}")
            return {"ok": True}
    else:
        return {"ok": True}

    ctx = {"telegram_id": from_id, "navn": user["navn"], "rolle": user["rolle"]}
    try:
        svar = run_agent(ctx, text)
    except Exception as e:
        log.exception("agent-fejl")
        svar = "Der opstod en fejl. Prøv igen om lidt."
        _notify_leader(f"Agent-fejl for {user['navn']}: {e}")

    telegram.send_message(chat["id"], svar)
    return {"ok": True}


def _notify_leader(text: str):
    """Safety net: send fejl til leder-gruppen."""
    if LEADER_GROUP_CHAT_ID:
        try:
            telegram.send_message(LEADER_GROUP_CHAT_ID, f"⚠️ Aura: {text}")
        except Exception:
            pass

"""FastAPI-indgang: modtager Telegram-webhooks, kører agenten, svarer."""
import logging
import re
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse

from .config import WEBHOOK_SECRET, LEADER_GROUP_CHAT_ID
from . import db, telegram, menu, reference
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


# ---------- Kundeportal: indtast referencenummer ----------

@app.get("/ref/{token}", response_class=HTMLResponse)
def ref_get(token: str):
    return reference.portal_page(token)


@app.post("/ref/{token}", response_class=HTMLResponse)
async def ref_post(token: str, request: Request):
    form = await request.form()
    return reference.submit_reference(token, form.get("reference", ""))


_seen_updates = []   # de seneste update_id'er vi har behandlet (mod Telegram-genforsøg -> dubletter)


def _already_handled(update_id):
    """True hvis vi allerede har behandlet denne besked (Telegram sender igen ved timeout)."""
    if update_id is None:
        return False
    if update_id in _seen_updates:
        return True
    _seen_updates.append(update_id)
    if len(_seen_updates) > 500:
        del _seen_updates[:250]
    return False


@app.post("/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(403, "forkert webhook-token")

    update = await request.json()
    if _already_handled(update.get("update_id")):
        return {"ok": True}   # samme besked igen -> ignorér (undgå dobbelt-behandling)

    # Knap-tryk (callback_query) — kun for leder
    cq = update.get("callback_query")
    if cq:
        u = db.get_user(str(cq.get("from", {}).get("id")))
        if u and u["rolle"] == "pro":
            try:
                menu.handle_callback(cq)
            except Exception as e:
                log.exception("menu-callback-fejl")
                _notify_leader(f"Menu-fejl: {e}")
        else:
            telegram.answer_callback(cq.get("id"), "Kun lederen har adgang.")
        return {"ok": True}

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    chat = msg.get("chat", {})
    if chat.get("type") != "private":   # Aura svarer kun i privat chat
        return {"ok": True}

    # Ignorér service-/system-beskeder (fx "pinned a message", medlem tilføjet) og
    # bot-beskeder — de har ingen rigtig afsender og må ikke udløse adgangs-afvisning.
    if not (msg.get("text") or msg.get("voice")):
        return {"ok": True}
    if msg.get("from", {}).get("is_bot"):
        return {"ok": True}

    from_id = str(msg["from"]["id"])
    user = db.get_user(from_id)
    if not user:
        telegram.send_message(chat["id"], "Du har ikke adgang til Aura. Kontakt din leder.")
        return {"ok": True}

    telegram.send_chat_action(chat["id"])   # vis "skriver…" med det samme

    # Tekst eller talebesked — talte beskeder besvares med tale, skrevne med tekst
    var_tale = bool(msg.get("voice"))
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

    # Menu-kommandoer (kun leder) — virker både skrevet og talt (fx "menu", "nye ordrer")
    if user["rolle"] == "pro" and menu.try_command(chat["id"], from_id, text):
        return {"ok": True}

    # Svarer brugeren (Telegram-reply) på en besked? Så er det ALTID en tilføjelse til en
    # eksisterende sag — aldrig en ny sag. Kan vi læse sagsnummeret, giver vi det med.
    reply = msg.get("reply_to_message")
    if reply and reply.get("text"):
        m = re.search(r"[Ss]ag\s+(\d+)", reply["text"])
        if m:
            text = (f"(Brugeren svarer på sag {m.group(1)} — beskeden er en TILFØJELSE til DEN sag, "
                    f"ikke en ny sag.) {text}")
        else:
            text = ("(Brugeren svarer på en tidligere besked — det er en TILFØJELSE til en "
                    "eksisterende sag, ALDRIG en ny sag. Er du i tvivl om hvilken sag, så spørg "
                    f"kort i stedet for at oprette noget.) {text}")

    ctx = {"telegram_id": from_id, "navn": user["navn"], "rolle": user["rolle"]}
    try:
        svar = run_agent(ctx, text)
    except Exception as e:
        log.exception("agent-fejl")
        svar = "Der opstod en fejl. Prøv igen om lidt."
        _notify_leader(f"Agent-fejl for {user['navn']}: {e}")

    # Talte du til hende -> svar med tale; ellers tekst (sparer data ved skrift)
    if var_tale:
        try:
            telegram.send_voice(chat["id"], telegram.synthesize_voice(svar))
        except Exception as e:
            log.exception("tts-fejl")
            telegram.send_message(chat["id"], svar)  # fald tilbage til tekst
            _notify_leader(f"TTS-fejl: {e}")
    else:
        telegram.send_message(chat["id"], svar)
    return {"ok": True}


def _notify_leader(text: str):
    """Safety net: send fejl til leder-gruppen."""
    if LEADER_GROUP_CHAT_ID:
        try:
            telegram.send_message(LEADER_GROUP_CHAT_ID, f"⚠️ Aura: {text}")
        except Exception:
            pass

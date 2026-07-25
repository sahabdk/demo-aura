"""FastAPI-indgang: modtager Telegram-webhooks, kører agenten, svarer."""
import logging
import re
import time as _time
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


# ---------- Retell-telefonagent + adresse-portal ----------

@app.post("/retell/{secret}/inbound")
async def retell_inbound(secret: str, request: Request):
    """Kaldes af Retell FØR samtalen: slå opkalderen op og returnér dynamiske variabler."""
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(403, "forkert token")
    data = await request.json()
    fra = ((data.get("call_inbound") or {}).get("from_number")) or ""
    from . import retell as _retell
    variabler = _retell.inbound_vars(fra)
    print(f"[retell] inbound {fra} -> kunde_fundet={variabler.get('kunde_fundet')}", flush=True)
    return {"call_inbound": {"dynamic_variables": variabler}}


@app.post("/retell/{secret}/webhook")
async def retell_webhook(secret: str, request: Request):
    """Kaldes af Retell efter opkaldet (event: call_analyzed)."""
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(403, "forkert token")
    data = await request.json()
    if data.get("event") == "call_analyzed":
        from . import retell as _retell
        try:
            _retell.haandter_afsluttet_opkald(data)
        except Exception as e:
            log.exception("retell-webhook-fejl")
            _notify_leader(f"Telefon-webhook-fejl: {e}")
    return {"ok": True}


@app.get("/adr/{token}", response_class=HTMLResponse)
def adr_get(token: str):
    from . import retell as _retell
    return _retell.adr_side(token)


@app.post("/adr/{token}", response_class=HTMLResponse)
async def adr_post(token: str, request: Request):
    from . import retell as _retell
    form = await request.form()
    return _retell.adr_submit(token, form)


_seen_updates = []   # de seneste update_id'er vi har behandlet (mod Telegram-genforsøg -> dubletter)
_afventende_foto = {}   # chat_id -> (file_id, tidspunkt): foto der venter på et sagsnummer


def _foto_uden_nummer(chat_id, from_id, user, file_id, caption):
    """Intet sagsnummer i billedteksten: proev at finde sagen ud fra adresse/kundenavn."""
    sagsnr, forslag = None, []
    try:
        from . import tools as _tools
        sagsnr, forslag = _tools.find_sag_ud_fra_tekst(caption)
    except Exception:
        log.exception("foto-adresseopslag-fejl")
    if sagsnr:
        _gem_foto_paa_sag(chat_id, from_id, user, file_id, str(sagsnr), caption)
        return
    _afventende_foto[str(chat_id)] = (file_id, _time.time())
    if forslag:
        linjer = [f"- sag {f['sagsnummer']}: {f['kunde']}, {f['adresse']} — {f['beskrivelse']}".rstrip(" —")
                  for f in forslag]
        telegram.send_message(chat_id, "Hvilken sag skal billedet gemmes på?\n" + "\n".join(linjer)
                                       + "\nSvar bare med sagsnummeret.")
    else:
        telegram.send_message(chat_id, "Hvilken sag skal billedet gemmes på? "
                                       "Svar bare med sagsnummeret (fx 132).")


def _gem_foto_paa_sag(chat_id, from_id, user, file_id, sag, caption=""):
    """Hent fotoet fra Telegram og gem det i sagens Dokumentation (med alle tjek)."""
    if db.get_meta("aura_pauseret") == "1":
        telegram.send_message(chat_id, "⏸ Aura er sat på pause af lederen — jeg må ikke gemme noget lige nu.")
        return
    from . import tools as _tools
    afvist = _tools._ejer_eller_afvis(sag, {"telegram_id": from_id, "navn": user["navn"],
                                            "rolle": user["rolle"]})
    if afvist:
        telegram.send_message(chat_id, afvist.get("resultat") or "Afvist.")
        return
    try:
        data = telegram.download_file(file_id)
        from . import os_graphql as os_gql
        from .config import now_local
        navn = f"aura_{now_local().strftime('%Y%m%d_%H%M%S')}.jpg"
        _up = os_gql.upload_case_document(sag, navn, data, description=caption)
        try:
            db.log_handling(from_id, user["navn"], user["rolle"], "foto gemt på sag",
                            f"sag {sag}: {navn}", ref_type="dokument", ref_id=(_up or {}).get("id"))
        except Exception:
            pass
        telegram.send_message(chat_id, f"📎 Billedet er gemt under Dokumentation på sag {sag}.")
    except Exception as e:
        log.exception("dokument-upload-fejl")
        telegram.send_message(chat_id, f"Kunne ikke gemme billedet på sag {sag} — prøv igen.")
        _notify_leader(f"Dokument-upload-fejl: {e}")


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
    if not (msg.get("text") or msg.get("voice") or msg.get("photo")):
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
    elif msg.get("photo"):
        # Foto: enten stregkode-scanning af en vare ELLER "gem billedet paa sag N" (Dokumentation)
        caption = (msg.get("caption") or "").strip()
        cl = caption.lower()
        fil_id = msg["photo"][-1]["file_id"]
        # "sag/ordre 132" i alle bøjninger
        m_sag = re.search(r"(?:sag|ordre)\w*\.?\s*(\d+)", cl)
        vil_gemme = any(w in cl for w in ("dok", "gem", "upload", "vedhæft", "vedhaeft", "arkiv",
                                          "billed", "foto"))
        naevner_ordre = any(w in cl for w in ("tilføj", "tilfoej", "ordre", "sag"))
        if (vil_gemme or naevner_ordre) and not m_sag:
            # intet nummer -> arv evt. sagsnummer fra beskeden der svares på
            rep = msg.get("reply_to_message") or {}
            rep_tekst = f"{rep.get('text') or ''} {rep.get('caption') or ''}"
            m_sag = re.search(r"[Ss]ag\w*\.?\s*(\d+)", rep_tekst)
        if vil_gemme and m_sag:
            _gem_foto_paa_sag(chat["id"], from_id, user, fil_id, m_sag.group(1), caption)
            return {"ok": True}
        if vil_gemme and not m_sag:
            _foto_uden_nummer(chat["id"], from_id, user, fil_id, caption)
            return {"ok": True}
        # Stregkode-scanning (ellers)
        try:
            data = telegram.download_file(fil_id)
            from . import stregkode
            kode = stregkode.find_stregkode(data)
        except Exception as e:
            log.exception("stregkode-fejl")
            telegram.send_message(chat["id"], "Jeg kunne ikke behandle billedet — prøv igen.")
            _notify_leader(f"Stregkode-fejl: {e}")
            return {"ok": True}
        if not kode:
            # Ingen stregkode - men naevner teksten en ordre/sag, saa var det nok et
            # dokumentations-billede ("tilfoej til ordren paa torvet 6")
            if m_sag:
                _gem_foto_paa_sag(chat["id"], from_id, user, fil_id, m_sag.group(1), caption)
                return {"ok": True}
            if naevner_ordre:
                _foto_uden_nummer(chat["id"], from_id, user, fil_id, caption)
                return {"ok": True}
            telegram.send_message(chat["id"], "Jeg kunne ikke finde en stregkode på billedet. "
                                              "Prøv tættere på, i bedre lys, og hold koden fladt. "
                                              "Ville du gemme billedet på en sag, så skriv fx "
                                              "'gem på sag 129' eller 'gem på ordren hos [kunde/adresse]' "
                                              "som billedtekst.")
            return {"ok": True}
        text = f"(Brugeren har scannet en vare-stregkode: {kode}.) "
        if caption:
            text += caption
        else:
            text += ("Find varen ud fra stregkode-nummeret (soeg med nummeret) og spoerg "
                     "hvilken sag og hvor mange styk den skal paa.")
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

    # Venter et foto på et sagsnummer? Så er "132" / "gem på sag 132" svaret på DET.
    afv_foto = _afventende_foto.get(str(chat["id"]))
    if afv_foto and _time.time() - afv_foto[1] < 600:
        tl = text.strip().lower()
        # Hun har LIGE spurgt "hvilken sag?" - ethvert kort svar med et tal er sagsnummeret
        # (accepterer "132", "ja 132", "sag 132", "på sagen 132 tak" osv.)
        mnum = re.search(r"(\d{1,6})", tl) if len(tl) < 40 else None
        if mnum:
            _afventende_foto.pop(str(chat["id"]), None)
            _gem_foto_paa_sag(chat["id"], from_id, user, afv_foto[0], mnum.group(1))
            return {"ok": True}
    elif afv_foto:
        _afventende_foto.pop(str(chat["id"]), None)   # udløbet

    # Menu-kommandoer (kun leder) — virker både skrevet og talt (fx "menu", "nye ordrer")
    if user["rolle"] == "pro" and menu.try_command(chat["id"], from_id, text):
        return {"ok": True}

    # Svarer brugeren (Telegram-reply) på en besked? Så er det ALTID en tilføjelse til en
    # eksisterende sag — aldrig en ny sag. Kan vi læse sagsnummeret, giver vi det med.
    reply = msg.get("reply_to_message")
    if reply and reply.get("text"):
        rt = reply["text"]
        if "Telefonbesked fra AI-Aura" in rt or "Adresse modtaget" in rt:
            # Svar på en telefonbesked: giv agenten ALLE oplysningerne fra den
            text = ("(Brugeren svarer på denne telefonbesked fra telefon-agenten:\n---\n"
                    + rt[:700] + "\n---\n"
                    "Brug oplysningerne fra beskeden DIREKTE. VIGTIGST: Står der 'Kundenr.:' i "
                    "beskeden, FINDES kunden allerede i ordrestyring — brug DET kundenummer direkte "
                    "til opret_sag, og opret ALDRIG en ny kunde. Kun hvis der IKKE står et kundenr. "
                    "(ukendt kunde): find kunden med soeg_kunde på telefonnummer/navn, og opret den "
                    "kun hvis søgningen intet giver. Brug beskedens indhold som sagens beskrivelse. "
                    "Nævner brugeren en medarbejder, så tildel sagen med tildel_sag. Spørg KUN om "
                    "det der reelt mangler, og tilbyd INGEN ekstra-handlinger: er kunden kendt "
                    "(Kundenr. i beskeden), er telefonnummeret allerede registreret — tilbyd "
                    "ALDRIG at gemme det igen. Afslut med en kort bekræftelse.) "
                    + text)
        else:
            m = re.search(r"[Ss]ag\s+(\d+)", rt)
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

"""Fejlmeldinger til Nila-teamet (kontakt@nila.dk) - så vi kan finde og rette fejlen.

Når Nila ikke har kunnet udføre det brugeren bad om, siger hun det ærligt og tilbyder en fejlmelding.
Siger brugeren ja (eller beder selv om det), samles en DETALJERET rapport:
  - firma, bruger, tidspunkt og den kørende version (Railway commit)
  - brugerens egen beskrivelse af hvad der gik galt
  - de seneste beskeder i samtalen
  - de seneste værktøjskald med argumenter og svar (hvad Nila faktisk forsøgte - og hvad der kom tilbage)
  - de seneste handlinger/fejl i handlingsloggen
Rapporten sendes på mail til FEJL_EMAIL (standard kontakt@nila.dk) via app.email (Resend/SMTP), og
gemmes altid i handlingsloggen (Pilly -> Fejl). Er FEJL_TELEGRAM_ID sat, får den person også et kort resumé.
"""
import json
import os
import time

from . import db

FEJL_EMAIL = os.environ.get("FEJL_EMAIL", "kontakt@nila.dk")
_SPOR_MAKS = 25


def spor(telegram_id, navn, args, resultat):
    """Gem ét værktøjskald i brugerens spor (de seneste 25) - bruges i fejlrapporten."""
    try:
        k = f"vaerktoejs_spor:{telegram_id}"
        s = json.loads(db.get_meta(k) or "[]")
        s.append({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "v": navn,
                  "args": json.dumps(args, ensure_ascii=False)[:600],
                  "svar": json.dumps(resultat, ensure_ascii=False, default=str)[:900]})
        db.set_meta(k, json.dumps(s[-_SPOR_MAKS:], ensure_ascii=False))
    except Exception:
        pass


def _navne():
    """(firma, assistent) - virker både i Nila-koden (config har FIRMA_NAVN) og i Aura-koden (Vandt & Vandt)."""
    try:
        from . import config as _c
    except Exception:
        _c = None
    firma = (getattr(_c, "FIRMA_NAVN", "") or os.environ.get("FIRMA_NAVN")
             or os.environ.get("RAILWAY_PROJECT_NAME") or "ukendt firma")
    assistent = getattr(_c, "ASSISTENT_NAVN", "") or os.environ.get("ASSISTENT_NAVN") or "Aura"
    return firma, assistent


def _now():
    try:
        from .config import now_local
        return now_local()
    except Exception:
        from datetime import datetime
        return datetime.now()


def _version():
    for k in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT", "SOURCE_VERSION"):
        if os.environ.get(k):
            return os.environ[k][:10]
    return "ukendt"


def lav_rapport(ctx, beskrivelse):
    firma, assistent = _navne()
    tid = str(ctx.get("telegram_id") or "")
    linjer = [
        f"FEJLMELDING fra {assistent} hos {firma}",
        f"Tidspunkt: {_now():%Y-%m-%d %H:%M}",
        f"Bruger: {ctx.get('navn')} (rolle {ctx.get('rolle')}, telegram {tid})",
        f"Version: {_version()}",
        "",
        "HVAD GIK GALT (brugerens/Nilas beskrivelse):",
        (beskrivelse or "(ingen beskrivelse)").strip(),
        "",
        "SENESTE BESKEDER I SAMTALEN:",
    ]
    try:
        for m in db.recent_messages(tid, limit=12):
            linjer.append(f"  {m.get('role')}: {str(m.get('content') or '')[:1200]}")
    except Exception as e:
        linjer.append(f"  (kunne ikke hentes: {e})")
    linjer += ["", "SENESTE VÆRKTØJSKALD (hvad Nila forsøgte, og hvad der kom tilbage):"]
    try:
        s = json.loads(db.get_meta(f"vaerktoejs_spor:{tid}") or "[]")
        for e in s[-15:]:
            linjer.append(f"  [{e['t']}] {e['v']}({e['args']})")
            linjer.append(f"      -> {e['svar']}")
        if not s:
            linjer.append("  (ingen)")
    except Exception as e:
        linjer.append(f"  (kunne ikke hentes: {e})")
    linjer += ["", "SENESTE HANDLINGER/FEJL I LOGGEN:"]
    try:
        for h in db.handlinger_seneste(20):
            linjer.append(f"  [{h.get('ts')}] {h.get('navn')}: {h.get('handling')} - {str(h.get('detaljer') or '')[:300]}")
    except Exception as e:
        linjer.append(f"  (kunne ikke hentes: {e})")
    return "\n".join(linjer)


def send(ctx, beskrivelse):
    """-> {'status': 'sendt'|'gemt', 'til': ..., 'besked': ...}"""
    firma, assistent = _navne()
    rapport = lav_rapport(ctx, beskrivelse)
    emne = f"Fejlmelding: {assistent} hos {firma} - {(beskrivelse or '').strip()[:60]}"
    try:
        db.log_handling(ctx.get("telegram_id"), ctx.get("navn"), ctx.get("rolle"), "fejlmelding",
                        (beskrivelse or "")[:380])
    except Exception:
        pass
    status = "gemt"
    try:
        from . import email as _mail
        if _mail._send(FEJL_EMAIL, emne, rapport) == "sent":
            status = "sendt"
    except Exception as e:
        print(f"[fejlrapport] mail fejlede: {str(e)[:200]}", flush=True)
    print(f"[fejlrapport] {status}\n{rapport[:3000]}", flush=True)   # står altid i Railway-loggen
    tg = os.environ.get("FEJL_TELEGRAM_ID", "").strip()
    if tg:
        try:
            from . import telegram
            telegram.send_message(tg, f"🐞 Fejlmelding fra {firma} ({ctx.get('navn')}): "
                                      f"{(beskrivelse or '')[:500]}\nMail: {status}")
        except Exception:
            pass
    return {"status": status, "til": FEJL_EMAIL}

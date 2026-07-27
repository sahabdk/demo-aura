"""Planlagte opgaver (erstatter Make's planlagte scenarier).

- Morgen-oversigt over dagens aftaler pr. bruger (kl. 07).
- Påmindelse om aftaler der starter snart (hvert 15. min i arbejdstiden).
- Ugentlig oversigt over forfaldne, ubetalte fakturaer til leder-gruppen (man kl. 08).
"""
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

from .config import TZ, LEADER_GROUP_CHAT_ID, now_local
from . import db, telegram, ordrestyring as os_api

log = logging.getLogger("aura.scheduler")


def morning_digest():
    nu = now_local()
    print(f"[morgen] morgen-oversigt koerer nu (serverens lokale tid: {nu:%Y-%m-%d %H:%M})", flush=True)
    today = nu.strftime("%Y-%m-%d")
    for u in db.all_users():
        # kun aftaler der IKKE allerede er passeret
        rows = db.appointments_between(u["telegram_id"],
                                       nu.isoformat(timespec="seconds"), today + "T23:59:59")
        linjer = []
        for r in rows:
            tid = (r.get("start") or "")[11:16]
            # normalisér AL slags mellemrum/usynlige tegn - tomme aftaler skal aldrig med
            tekst = " ".join(f"{r.get('kunde') or ''} {r.get('opgave') or ''}".split())
            if len(tekst) < 2:
                continue   # aftale uden reelt indhold -> spring over
            linjer.append((f"- kl. {tid} {tekst}".rstrip() if tid else f"- {tekst}"))
        if not linjer:      # ingen RIGTIGE aftaler -> ingen besked
            continue
        try:
            telegram.send_message(u["telegram_id"], "🔔 Dine aftaler i dag:\n" + "\n".join(linjer))
        except Exception:
            log.exception("kunne ikke sende morgen-oversigt til %s", u["telegram_id"])


def soon_reminders():
    """Minder om aftaler der starter inden for ~15 min. Sender KUN én gang pr. aftale (mindet=1)."""
    now = now_local()
    soon = now + timedelta(minutes=15)
    for r in db.due_reminders(now.isoformat(timespec="seconds"), soon.isoformat(timespec="seconds")):
        tekst = " ".join(f"{r.get('kunde') or ''} {r.get('opgave') or ''}".split())
        if len(tekst) < 2:   # tom aftale -> ingen paamindelse, men markér saa den ikke spoeger igen
            try:
                db.mark_reminded(r["id"])
            except Exception:
                pass
            continue
        besked = f"🔔 Om lidt kl. {r['start'][11:16]}: {tekst}".rstrip()
        try:
            telegram.send_message(r["telegram_id"], besked)
            db.mark_reminded(r["id"])
        except Exception:
            log.exception("kunne ikke sende påmindelse")


def faktura_overview():
    """Mandags-oversigt: kun hvis der er forfaldne fakturaer (ellers tavst).
    Viser kort med detaljer + 'Send rykker'-knap pr. faktura (samme som menuen)."""
    if not LEADER_GROUP_CHAT_ID:
        return
    from .tools import forfaldne_fakturaer
    if not forfaldne_fakturaer({}, {}).get("fakturaer"):
        return
    from . import menu
    telegram.send_message(LEADER_GROUP_CHAT_ID, "God morgen! Her er ugens forfaldne fakturaer:")
    menu.vis_forfaldne(LEADER_GROUP_CHAT_ID)


def reference_scan():
    """Scan de store kunders sager for manglende referencenumre og mail dem."""
    if not db.funktion_til("referencescan"):
        return
    try:
        from . import reference
        reference.scan_and_notify()
    except Exception:
        log.exception("reference-scan fejlede")


def status_vagt():
    """Hver time: Åbne sager hvor den planlagte starttid er nået, skiftes automatisk
    til 'Igangværende'. Lukket/Aflyst/øvrige statusser røres aldrig."""
    import time as _t
    if not db.funktion_til("statusvagt"):
        return   # slaaet fra i kontrolpanelet
    if db.get_meta("aura_pauseret") == "1":
        print("[status_vagt] springes over — Aura er sat på pause", flush=True)
        return
    try:
        statusser = os_api.case_statuses()
        aaben_id = next((s.get("id") for s in statusser
                         if (s.get("text") or "").strip().lower() in ("åben", "aaben", "open")), None)
        igang_id = next((s.get("id") for s in statusser
                         if "igang" in (s.get("text") or "").lower()), None)
        if aaben_id is None or igang_id is None:
            print(f"[status_vagt] fandt ikke status-id'er (aaben={aaben_id}, igang={igang_id})", flush=True)
            return
        from . import os_graphql as os_gql
        nu = int(_t.time())
        MAX_SKIFT = db.graense("statusvagt", 15)   # justerbart loft (Pilly-dashboardet)
        skiftet = []
        for c in os_api.cases_paged():
            if len(skiftet) >= MAX_SKIFT:
                print(f"[status_vagt] STOP: naaede loftet paa {MAX_SKIFT} skift i en koersel", flush=True)
                if LEADER_GROUP_CHAT_ID:
                    telegram.send_message(LEADER_GROUP_CHAT_ID,
                                          f"⚠️ Status-vagten stoppede efter {MAX_SKIFT} automatiske skift i én "
                                          "kørsel (sikkerhedsloft). Tjek at planlægningen ser rigtig ud — resten "
                                          "tages i næste kørsel.")
                break
            if str(c.get("status")) != str(aaben_id):
                continue
            nr = c.get("case_number")
            try:
                evts = os_gql.planned_events(nr)
            except Exception as e:
                print(f"[status_vagt] plan-opslag fejlede for sag {nr}: {str(e)[:150]}", flush=True)
                continue
            if any(int(e.get("startTime") or 0) and int(e.get("startTime") or 0) <= nu for e in evts):
                try:
                    os_api.set_case_status(nr, igang_id)
                    skiftet.append(str(nr))
                    print(f"[status_vagt] sag {nr} -> Igangværende (planlagt tid nået)", flush=True)
                    try:
                        db.log_handling("", "Aura (automatisk)", "system",
                                        "status skiftet til Igangværende", f"sag {nr} (planlagt tid nået)")
                    except Exception:
                        pass
                except Exception as e:
                    print(f"[status_vagt] kunne ikke skifte sag {nr}: {str(e)[:150]}", flush=True)
        if skiftet and LEADER_GROUP_CHAT_ID:
            telegram.send_leader(LEADER_GROUP_CHAT_ID,
                                  "🔄 Automatisk status: sag " + ", ".join(skiftet)
                                  + " er nu Igangværende (planlagt tid nået).")
    except Exception as e:
        print(f"[status_vagt] fejl: {str(e)[:200]}", flush=True)


def start_scheduler():
    sch = BackgroundScheduler(timezone=TZ)
    sch.add_job(morning_digest, "cron", hour=7, minute=0)
    sch.add_job(soon_reminders, "cron", minute="*/5")   # hele døgnet, alle dage
    sch.add_job(faktura_overview, "cron", day_of_week="mon", hour=8, minute=0)
    sch.add_job(reference_scan, "cron", day_of_week="mon-fri", hour="7-18", minute=0)  # hver hele time i arbejdstiden
    sch.add_job(status_vagt, "cron", minute="*/15")   # hvert 15. min: Åben -> Igangværende når planlagt tid er nået
    sch.start()
    log.info("scheduler kører")
    try:
        from datetime import datetime as _dt
        print(f"[scheduler] TZ={TZ}, server-tid={_dt.now().isoformat(timespec='seconds')}, "
              f"dansk tid={now_local().isoformat(timespec='seconds')}", flush=True)
        for job in sch.get_jobs():
            print(f"[scheduler] {job.func.__name__}: næste kørsel {job.next_run_time}", flush=True)
    except Exception:
        pass
    return sch

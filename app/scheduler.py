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
    today = now_local().strftime("%Y-%m-%d")
    for u in db.all_users():
        rows = db.appointments_between(u["telegram_id"], today + "T00:00:00", today + "T23:59:59")
        if not rows:
            continue
        linjer = [f"- kl. {r['start'][11:16]} {r['kunde'] or ''} {r['opgave'] or ''}".rstrip() for r in rows]
        try:
            telegram.send_message(u["telegram_id"], "🔔 Dine aftaler i dag:\n" + "\n".join(linjer))
        except Exception:
            log.exception("kunne ikke sende morgen-oversigt til %s", u["telegram_id"])


def soon_reminders():
    """Minder om aftaler der starter inden for ~15 min. Sender KUN én gang pr. aftale (mindet=1)."""
    now = now_local()
    soon = now + timedelta(minutes=15)
    for r in db.due_reminders(now.isoformat(timespec="seconds"), soon.isoformat(timespec="seconds")):
        besked = f"🔔 Om lidt kl. {r['start'][11:16]}: {r.get('kunde') or ''} {r.get('opgave') or ''}".rstrip()
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
    if not os_api.overdue_unpaid_invoices():
        return
    from . import menu
    telegram.send_message(LEADER_GROUP_CHAT_ID, "God morgen! Her er ugens forfaldne fakturaer:")
    menu.vis_forfaldne(LEADER_GROUP_CHAT_ID)


def reference_scan():
    """Scan de store kunders sager for manglende referencenumre og mail dem."""
    try:
        from . import reference
        reference.scan_and_notify()
    except Exception:
        log.exception("reference-scan fejlede")


def start_scheduler():
    sch = BackgroundScheduler(timezone=TZ)
    sch.add_job(morning_digest, "cron", hour=7, minute=0)
    sch.add_job(soon_reminders, "cron", minute="*/5")   # hele døgnet, alle dage
    sch.add_job(faktura_overview, "cron", day_of_week="mon", hour=8, minute=0)
    sch.add_job(reference_scan, "cron", day_of_week="mon-fri", hour="7-18", minute=0)  # hver hele time i arbejdstiden
    sch.start()
    log.info("scheduler kører")
    return sch

"""Planlagte opgaver (erstatter Make's planlagte scenarier).

- Morgen-oversigt over dagens aftaler pr. bruger (kl. 07).
- Påmindelse om aftaler der starter snart (hvert 15. min i arbejdstiden).
- Ugentlig oversigt over forfaldne, ubetalte fakturaer til leder-gruppen (man kl. 08).
"""
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

from .config import TZ, LEADER_GROUP_CHAT_ID
from . import db, telegram, ordrestyring as os_api

log = logging.getLogger("aura.scheduler")


def morning_digest():
    today = datetime.now().strftime("%Y-%m-%d")
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
    now = datetime.now()
    soon = now + timedelta(minutes=15)
    for u in db.all_users():
        rows = db.appointments_between(u["telegram_id"], now.isoformat(timespec="seconds"),
                                       soon.isoformat(timespec="seconds"))
        for r in rows:
            try:
                telegram.send_message(u["telegram_id"],
                                      f"🔔 Om lidt kl. {r['start'][11:16]}: {r['kunde'] or ''} {r['opgave'] or ''}".rstrip())
            except Exception:
                log.exception("kunne ikke sende påmindelse")


def faktura_overview():
    if not LEADER_GROUP_CHAT_ID:
        return
    rows = os_api.overdue_unpaid_invoices()
    if not rows:
        return
    linjer = [f"- {r.get('cust_name')} – sag {r.get('case_number')} – {r.get('amount_vat')} kr" for r in rows]
    telegram.send_message(LEADER_GROUP_CHAT_ID, "🧾 Forfaldne ubetalte fakturaer:\n" + "\n".join(linjer))


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
    sch.add_job(soon_reminders, "cron", day_of_week="mon-fri", hour="6-18", minute="*/15")
    sch.add_job(faktura_overview, "cron", day_of_week="mon", hour=8, minute=0)
    sch.add_job(reference_scan, "cron", day_of_week="mon-fri", hour="7-18", minute=0)  # hver hele time i arbejdstiden
    sch.start()
    log.info("scheduler kører")
    return sch

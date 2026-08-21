"""Planlagte opgaver (erstatter Make's planlagte scenarier).

- Morgen-oversigt over dagens aftaler pr. bruger (kl. 07).
- Påmindelse om aftaler der starter snart (hvert 15. min i arbejdstiden).
- Ugentlig oversigt over forfaldne, ubetalte fakturaer til leder-gruppen (man kl. 08).
"""
import logging
import os
import re
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
    """Mandags-oversigt over forfaldne fakturaer - tavs hvis der ingen er.
    Sendes som EMAIL til lederen (leder_email i dashboardet / LEADER_EMAIL i Railway).
    Er ingen leder-mail sat, sendes den i Telegram direkte til alle ledere."""
    from .tools import forfaldne_fakturaer
    fakturaer = forfaldne_fakturaer({}, {}).get("fakturaer") or []
    if not fakturaer:
        return
    leder_mail = (db.get_meta("leder_email") or os.environ.get("LEADER_EMAIL", "")).strip()
    if leder_mail:
        linjer = []
        for f in fakturaer[:50]:
            dage = f.get("dage_forsinket")
            rykk = f.get("antal_rykkere") or 0
            linjer.append(f"- {f['kunde']}: {f['beloeb']} kr, forfald {f['forfald']}"
                          + (f", {dage} dage forsinket" if dage else "")
                          + (f", {rykk} rykker(e) sendt" if rykk else ", ingen rykker sendt"))
        tekst = (f"God morgen.\n\nDer er {len(fakturaer)} forfaldne, ubetalte fakturaer:\n\n"
                 + "\n".join(linjer)
                 + "\n\nSend rykkere via Aura i Telegram: skriv 'forfaldne fakturaer'.")
        try:
            from .email import _send
            _send(leder_mail, "Forfaldne fakturaer - ugens overblik", tekst)
            db.log_handling("", "Aura", "system", "faktura-ugeoversigt mailet",
                            f"{leder_mail}: {len(fakturaer)} fakturaer")
            return
        except Exception:
            log.exception("faktura-mail fejlede - falder tilbage til Telegram")
    from . import menu
    for u in db.all_users():
        if u.get("rolle") == "pro":
            telegram.send_message(u["telegram_id"], "God morgen! Her er ugens forfaldne fakturaer:")
            menu.vis_forfaldne(u["telegram_id"])


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
    # Status-vagten er SLUKKET som standard (kunden oensker ikke auto-statusskift).
    # Den koerer KUN ved aktivt tilvalg i dashboardet - og overlever dermed ogsaa
    # en volume-wipe som slukket.
    if db.get_meta("funk_statusvagt") != "1":
        return
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
                if True:
                    telegram.send_leader(LEADER_GROUP_CHAT_ID,
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
        if skiftet:
            telegram.send_leader(LEADER_GROUP_CHAT_ID,
                                  "🔄 Automatisk status: sag " + ", ".join(skiftet)
                                  + " er nu Igangværende (planlagt tid nået).")
    except Exception as e:
        print(f"[status_vagt] fejl: {str(e)[:200]}", flush=True)


def stamdata_tjek():
    """LUKKEDE + IGANGVÆRENDE sager: mangler kunden FAKTURA-EMAIL paa kundekortet
    (Fakturakontakt), faar de en SMS med et personligt link - svaret skrives direkte
    i faktura-emailfeltet. Lukkede: sms straks. Igangvaerende: foerst efter 3 dages
    karens (STAMDATA_IGANG_DAGE). KUN faktura-emailen tjekkes (hverken kunde-email
    eller adresse). Hver kunde spoerges hoejst EN gang."""
    if not db.funktion_til("stamdatatjek"):
        return
    import secrets as _secrets
    from . import retell
    app_url = os.environ.get("APP_BASE_URL", "").rstrip("/")
    if not app_url:
        return
    firma = os.environ.get("FIRMA_NAVN", "os")
    sendt = 0
    try:
        sager = os_api.recent_cases(days=14)
    except Exception:
        log.exception("stamdata: kunne ikke hente sager")
        return
    def _er_igang(case):
        sid = str(case.get("status"))
        for st in os_api.case_statuses():
            if str(st.get("id")) == sid:
                t = (st.get("text") or "").lower()
                return any(o in t for o in ("igang", "i gang", "påbegynd", "paabegynd"))
        return False

    KARENS_DAGE = int(os.environ.get("STAMDATA_IGANG_DAGE", "3"))
    set_kunder = set()
    for c in sager:
        # LUKKEDE sager: sms straks. IGANGVÆRENDE: foerst efter KARENS_DAGE
        # (regnet fra foerste gang scanningen SAA sagen som igangvaerende).
        lukket = os_api.is_closed(c)
        if not lukket:
            if not _er_igang(c):
                continue   # aabne og oevrige statusser roeres aldrig
            nr_sag = c.get("case_number")
            noegle = f"stamdata_igang_{nr_sag}"
            foerste = db.get_meta(noegle)
            if not foerste:
                db.set_meta(noegle, now_local().strftime("%Y-%m-%d"))
                continue   # karens starter nu - sms tidligst om KARENS_DAGE dage
            try:
                dage = (now_local().date()
                        - datetime.strptime(foerste, "%Y-%m-%d").date()).days
            except ValueError:
                dage = 0
            if dage < KARENS_DAGE:
                continue
        kn = str(c.get("customer_number") or "")
        if not kn or kn in set_kunder:
            continue
        set_kunder.add(kn)
        try:
            d = os_api.get_debtor(kn) or {}
        except Exception:
            continue
        # KUN faktura-emailen tjekkes (Fakturakontakt-feltet) - intet andet
        fak_felt = os_api.faktura_email_felt(d)
        if not fak_felt:
            db.log_handling("", "Aura (stamdata)", "system", "stamdata: faktura-emailfelt ukendt",
                            f"kunde {kn}: API'et viser intet faktura-emailfelt - springes over")
            continue
        if str(d.get(fak_felt) or "").strip():
            continue   # faktura-email findes allerede
        mangler = ["email"]
        tlf = (d.get("customer_mobile") or d.get("customer_telephone") or "").strip()
        if not tlf:
            db.log_handling("", "Aura (stamdata)", "system", "stamdata mangler - ingen tlf",
                            f"kunde {kn} ({d.get('customer_name')}): mangler {','.join(mangler)}")
            continue
        # TEST-SIKRING: er STAMDATA_TEST_TLF sat, sendes KUN til praecis det nummer
        # (beskytter mod testkonti fulde af tilfaeldige numre, der kan tilhoere rigtige folk)
        test_tlf = os.environ.get("STAMDATA_TEST_TLF", "").strip()
        if test_tlf and re.sub(r"\D", "", tlf)[-8:] != re.sub(r"\D", "", test_tlf)[-8:]:
            db.log_handling("", "Aura (stamdata)", "system", "stamdata springes over (test-sikring)",
                            f"kunde {kn} ({d.get('customer_name')}): tlf {tlf} matcher ikke "
                            f"STAMDATA_TEST_TLF")
            continue
        if db.adr_request_for_kunde(kn):
            db.log_handling("", "Aura (stamdata)", "system", "stamdata springes over",
                            f"kunde {kn} ({d.get('customer_name')}): allerede spurgt en gang "
                            f"- nulstil evt. via /admin/.../adr-nulstil/{kn}")
            continue   # aldrig sms-spam
        token = _secrets.token_urlsafe(16)
        db.create_adr_request(token, tlf, d.get("customer_name") or "",
                              kundenummer=kn, felter=",".join(mangler))
        link = f"{app_url}/adr/{token}"
        tekst = (db.get_meta("skabelon_sms_stamdata")
                 or f"Tak fordi du valgte {firma}. Vi mangler en emailadresse til "
                    "fakturering - udfyld den venligst her: {link}")
        try:
            ret = retell.send_sms(tlf, tekst.replace("{link}", link))
            if ret == "sent":
                db.log_handling("", "Aura (stamdata)", "system", "stamdata-sms sendt",
                                f"kunde {kn} ({d.get('customer_name')}): mangler "
                                f"{','.join(mangler)} -> {tlf}")
                sendt += 1
            else:
                # deaktiveret/dry-run: SMS'en naaede ALDRIG kunden -> fjern anmodningen
                # igen, saa kunden ikke blokeres af 'allerede spurgt'-reglen
                db.slet_adr_request(token)
                db.log_handling("", "Aura (stamdata)", "system", "stamdata-sms IKKE sendt",
                                f"kunde {kn}: [{ret}] - prøves igen når SMS er tændt")
        except Exception as e:
            db.slet_adr_request(token)
            db.log_handling("", "Aura (stamdata)", "system", "stamdata-sms fejl",
                            f"kunde {kn}: {str(e)[:120]} - prøves igen næste kørsel")
        if sendt >= 10:   # loft pr. koersel - ro paa
            break


def sundheds_vagt():
    """Hvert 10. min: koer sundhedstjek + kig efter nye fejl i handlingsloggen.
    Skifter en status (groen<->roed), eller dukker nye fejl op, sendes en mail
    til ALERT_EMAIL med titlen AURA FEJL. Foerste koersel saetter kun baseline."""
    import json as _json
    from . import sundhed, db
    from .email import _send_resend

    alert_til = os.environ.get("ALERT_EMAIL", "sahabdk@gmail.com")
    try:
        tjek = sundhed.alle_tjek()
    except Exception as e:
        print(f"[sundheds_vagt] tjek fejlede: {str(e)[:150]}", flush=True)
        return
    ny = {t["navn"]: {"ok": bool(t["ok"]), "detalje": t["detalje"]} for t in tjek}

    linjer = []
    try:
        gammel = _json.loads(db.get_meta("sundhed_status") or "null")
    except Exception:
        gammel = None
    if gammel is not None:
        for navn, s in ny.items():
            foer = (gammel.get(navn) or {}).get("ok")
            if foer is None or foer == s["ok"]:
                continue
            if s["ok"]:
                linjer.append(f"🟢 OK IGEN: {navn}")
            else:
                linjer.append(f"🔴 FEJL: {navn}\n   {s['detalje']}")

    # nye fejl-linjer i handlingsloggen siden sidste koersel
    sidste_fejl_ts = db.get_meta("sundhed_fejl_ts") or ""
    nyeste_ts = sidste_fejl_ts
    try:
        for h in db.handlinger_seneste(antal=150):
            ts = h.get("ts") or ""
            if "fejl" in (h.get("handling") or "").lower() and ts > sidste_fejl_ts:
                linjer.append(f"⚠️ NY FEJL i loggen ({ts}): {h.get('handling')} — "
                              f"{(h.get('detaljer') or '')[:200]}")
                nyeste_ts = max(nyeste_ts, ts)
    except Exception:
        pass

    db.set_meta("sundhed_status", _json.dumps(ny))
    if nyeste_ts != sidste_fejl_ts:
        db.set_meta("sundhed_fejl_ts", nyeste_ts)
    if gammel is None or not linjer:
        return

    kun_ok = all(l.startswith("🟢") for l in linjer)
    emne = "AURA OK IGEN" if kun_ok else "AURA FEJL"
    tekst = (f"{os.environ.get('FIRMA_NAVN', 'Aura')} — automatisk overvaagning "
             f"({len(linjer)} aendring(er)):\n\n" + "\n\n".join(linjer)
             + "\n\nSe detaljer i Pilly-dashboardet (🩺 Sundhed / 🚨 Fejl).")
    try:
        # bevidst UDEN testtilstand-filter: driftsalarmen skal altid frem
        _send_resend(alert_til, emne, tekst)
        print(f"[sundheds_vagt] alarm sendt til {alert_til}: {emne}", flush=True)
    except Exception as e:
        print(f"[sundheds_vagt] mail fejlede: {str(e)[:150]}", flush=True)


def start_scheduler():
    sch = BackgroundScheduler(timezone=TZ)
    sch.add_job(morning_digest, "cron", hour=7, minute=0)
    sch.add_job(soon_reminders, "cron", minute="*/5")   # hele døgnet, alle dage
    sch.add_job(faktura_overview, "cron", day_of_week="mon", hour=8, minute=0)
    sch.add_job(reference_scan, "cron", day_of_week="mon-fri", hour="7-18", minute=0)  # hver hele time i arbejdstiden
    sch.add_job(status_vagt, "cron", minute="*/15")   # hvert 15. min: Åben -> Igangværende når planlagt tid er nået
    sch.add_job(sundheds_vagt, "cron", minute="7,17,27,37,47,57")   # hvert 10. min: mail-alarm ved statusskift/nye fejl
    sch.add_job(stamdata_tjek, "cron", day_of_week="mon-fri", hour="9,13,16", minute=45)  # lukkede sager: manglende kundedata -> sms
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

"""Sundhedstjek af alle integrationer (til Pilly-dashboardets 🩺-fane)."""
import os
import time
import requests


def _tjek(navn, fn):
    t0 = time.time()
    try:
        detalje = fn() or "OK"
        return {"navn": navn, "ok": True, "detalje": str(detalje)[:150],
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"navn": navn, "ok": False, "detalje": str(e)[:220],
                "ms": int((time.time() - t0) * 1000)}


def alle_tjek():
    from . import ordrestyring as os_api
    from . import os_graphql as os_gql
    from . import economic, db, retell
    from .config import TELEGRAM_TOKEN

    ud = []
    ud.append(_tjek("ordrestyring (v2 REST)",
                    lambda: f"{len(os_api.case_statuses(force=True))} statusser hentet"))
    ud.append(_tjek("ordrestyring (kunder)",
                    lambda: f"{len(os_api.all_debtors(force=True))} kunder hentet"))
    ud.append(_tjek("ordrestyring (GraphQL)",
                    lambda: (os_gql._gql("{ __typename }"), "svarer")[1]))

    if economic.klar():
        ud.append(_tjek("e-conomic", lambda: (economic.self_test() or {}).get("firma") or "forbundet"))
    else:
        ud.append({"navn": "e-conomic", "ok": False,
                   "detalje": "ikke sat op (ECONOMIC_GRANT_TOKEN mangler) - ordrestyring bruges som fallback",
                   "ms": 0})

    def _resend():
        key = os.environ.get("RESEND_API_KEY")
        if not key:
            raise RuntimeError("ikke sat op - mails koerer i dry-run")
        r = requests.get("https://api.resend.com/domains",
                         headers={"Authorization": f"Bearer {key}"}, timeout=10)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}")
        doms = (r.json() or {}).get("data") or []
        verificerede = [d.get("name") for d in doms if d.get("status") == "verified"]
        afsender = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")
        domaene = afsender.split("@")[-1].lower()
        if domaene == "resend.dev":
            raise RuntimeError("EMAIL_FROM er ikke sat - mails kommer fra onboarding@resend.dev "
                               "(kun test, ryger ofte i spam)")
        if domaene not in [v.lower() for v in verificerede]:
            raise RuntimeError(f"afsender {afsender}, men domænet {domaene} er IKKE verificeret "
                               f"i Resend - mails afvises! ({len(doms)} domæne(r), "
                               f"{len(verificerede)} verificeret)")
        return f"afsender {afsender} - domæne verificeret ✓"
    ud.append(_tjek("Mail-afsender (Resend)", _resend))

    def _gw():
        if not retell.GATEWAYAPI_TOKEN:
            raise RuntimeError("ikke sat op - sms koerer i dry-run")
        r = requests.get("https://gatewayapi.com/rest/me",
                         auth=(retell.GATEWAYAPI_TOKEN, ""), timeout=10)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}")
        d = r.json() or {}
        return f"kredit: {d.get('credit', '?')} {d.get('currency', '')}"
    ud.append(_tjek("GatewayAPI (sms)", _gw))

    def _openai():
        from .config import OPENAI_API_KEY, OPENAI_MODEL
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY mangler - Aura kan IKKE svare!")
        r = requests.get(f"https://api.openai.com/v1/models/{OPENAI_MODEL}",
                         headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=25)
        if r.status_code == 401:
            raise RuntimeError("nøglen er UGYLDIG - Aura kan IKKE svare!")
        if r.status_code == 404:
            raise RuntimeError(f"modellen '{OPENAI_MODEL}' findes ikke - tjek OPENAI_MODEL")
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}")
        return f"nøgle gyldig, model {OPENAI_MODEL}"
    ud.append(_tjek("OpenAI (AI-hjernen)", _openai))

    def _tg():
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=10)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}")
        return "@" + (((r.json() or {}).get("result") or {}).get("username") or "?")
    ud.append(_tjek("Telegram-bot", _tg))

    def _tg_webhook():
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo", timeout=10)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}")
        info = (r.json() or {}).get("result") or {}
        url = info.get("url") or ""
        if not url:
            raise RuntimeError("INGEN webhook sat - botten modtager intet! Kør setWebhook-adressen (A8)")
        fejl = info.get("last_error_message")
        fejl_tid = info.get("last_error_date") or 0
        ventende = info.get("pending_update_count", 0)
        # Telegram husker den SIDSTE fejl laenge - kun roed hvis fejlen er frisk (< 15 min)
        if fejl and (time.time() - fejl_tid) < 900:
            raise RuntimeError(f"webhook-fejl for {int((time.time()-fejl_tid)/60)} min siden: "
                               f"{fejl} ({ventende} beskeder i kø)")
        return f"aktiv ({ventende} i kø)"
    ud.append(_tjek("Telegram-webhook", _tg_webhook))

    def _retell_seneste():
        for h in db.handlinger_seneste(antal=300):
            if h.get("handling") == "telefonbesked modtaget":
                return f"seneste opkald behandlet: {h.get('ts')}"
        return "webhook klar - ingen opkald registreret endnu"
    ud.append(_tjek("Telefon (Retell-webhook)", _retell_seneste))

    ud.append(_tjek("Database", lambda: f"{len(db.all_users())} aktive brugere"))

    # ---- 💰 Penge: saldo og forbrug ----
    def _twilio():
        sid = os.environ.get("TWILIO_SID")
        tok = os.environ.get("TWILIO_TOKEN")
        if not sid or not tok:
            raise RuntimeError("ikke sat op - tilføj TWILIO_SID + TWILIO_TOKEN i Railway "
                               "(forsiden af Twilio Console), så overvåges nummerets saldo")
        r = requests.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Balance.json",
                         auth=(sid, tok), timeout=10)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code} - forkert SID/token?")
        d = r.json() or {}
        saldo = float(d.get("balance") or 0)
        valuta = d.get("currency") or ""
        brugt = ""
        try:
            r2 = requests.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
                              "/Usage/Records/ThisMonth.json?Category=totalprice",
                              auth=(sid, tok), timeout=10)
            rec = ((r2.json() or {}).get("usage_records") or [{}])[0]
            brugt = f" - brugt denne md: {float(rec.get('price') or 0):.2f} {rec.get('price_unit', '')}"
        except Exception:
            pass
        if saldo < 50:
            raise RuntimeError(f"LAV saldo: {saldo:.2f} {valuta} - tank op, ellers dør "
                               f"telefonnummeret!{brugt}")
        return f"saldo {saldo:.2f} {valuta}{brugt}"
    ud.append(_tjek("💰 Twilio (telefonnummer)", _twilio))

    def _openai_forbrug():
        import datetime
        key = os.environ.get("OPENAI_ADMIN_KEY")
        if not key:
            raise RuntimeError("ikke sat op - lav en Admin key: platform.openai.com -> "
                               "indstillinger -> Organization -> Admin keys -> tilføj som "
                               "OPENAI_ADMIN_KEY i Railway, så vises månedens AI-forbrug")
        start = int(datetime.datetime.now(datetime.timezone.utc)
                    .replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        r = requests.get("https://api.openai.com/v1/organization/costs",
                         params={"start_time": start, "limit": 31},
                         headers={"Authorization": f"Bearer {key}"}, timeout=15)
        if r.status_code == 401:
            raise RuntimeError("nøglen er ikke en ADMIN-key (alm. API-nøgler kan ikke se forbrug)")
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}")
        total = 0.0
        for bucket in (r.json() or {}).get("data") or []:
            for res in bucket.get("results") or []:
                total += float(((res.get("amount") or {}).get("value")) or 0)
        return f"AI-forbrug denne måned: ${total:.2f}"
    ud.append(_tjek("💰 OpenAI-forbrug", _openai_forbrug))

    return ud

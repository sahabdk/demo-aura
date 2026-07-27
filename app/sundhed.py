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
        verificerede = sum(1 for d in doms if d.get("status") == "verified")
        return f"{len(doms)} domæne(r), {verificerede} verificeret"
    ud.append(_tjek("Resend (rykker-mails)", _resend))

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

    def _tg():
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=10)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}")
        return "@" + (((r.json() or {}).get("result") or {}).get("username") or "?")
    ud.append(_tjek("Telegram-bot", _tg))

    def _retell_seneste():
        for h in db.handlinger_seneste(antal=300):
            if h.get("handling") == "telefonbesked modtaget":
                return f"seneste opkald behandlet: {h.get('ts')}"
        return "webhook klar - ingen opkald registreret endnu"
    ud.append(_tjek("Telefon (Retell-webhook)", _retell_seneste))

    ud.append(_tjek("Database", lambda: f"{len(db.all_users())} aktive brugere"))
    return ud

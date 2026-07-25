"""e-conomic REST-klient: forfaldne, ubetalte fakturaer (betalingsstatus fra bogholderiet).

Auth: to tokens i headers — X-AppSecretToken (app-nøgle) og X-AgreementGrantToken
(kundens adgang). Sættes i Railway som ECONOMIC_APP_TOKEN og ECONOMIC_GRANT_TOKEN.
"""
import requests
from datetime import date, datetime
from .config import ECONOMIC_APP_TOKEN, ECONOMIC_GRANT_TOKEN

BASE = "https://restapi.e-conomic.com"
TIMEOUT = 30


def klar():
    """Er e-conomic sat op (token i miljøet)?"""
    return bool(ECONOMIC_GRANT_TOKEN)


def _headers():
    return {"X-AppSecretToken": ECONOMIC_APP_TOKEN or "demo",
            "X-AgreementGrantToken": ECONOMIC_GRANT_TOKEN,
            "Content-Type": "application/json"}


def _get(path, params=None):
    r = requests.get(f"{BASE}{path}", params=params, headers=_headers(), timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"e-conomic GET {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def self_test():
    """Hvem er vi logget ind som? (firma + aftalenummer) — til 'test economic'-kommandoen."""
    d = _get("/self")
    return {"aftale": d.get("agreementNumber"),
            "firma": (d.get("company") or {}).get("name"),
            "bruger": (d.get("user") or {}).get("email")}


def overdue_invoices():
    """Bogførte fakturaer med restbeløb > 0 og overskredet forfaldsdato (= reelt ubetalte)."""
    idag = date.today().isoformat()
    rows, side = [], 0
    while side < 10:   # sikkerhedsloft: max 10 sider a 1000
        d = _get("/invoices/booked", params={
            "filter": f"remainder$gt:0$and:dueDate$lt:{idag}",
            "skippages": side, "pagesize": 1000,
        })
        batch = d.get("collection") or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
        side += 1
    ud = []
    for r in rows:
        due = r.get("dueDate") or ""
        dage = None
        try:
            dage = max(0, (datetime.now() - datetime.fromisoformat(due)).days)
        except (ValueError, TypeError):
            pass
        ud.append({
            "kunde": (r.get("recipient") or {}).get("name"),
            "kundenummer": (r.get("customer") or {}).get("customerNumber"),
            "fakturanummer": r.get("bookedInvoiceNumber"),
            "beloeb": r.get("remainder"),
            "valuta": r.get("currency"),
            "forfald": due,
            "dage_forsinket": dage,
        })
    return ud

"""Klient til ordrestyring.dk API v2.

HTTP Basic auth: API-nøglen er brugernavn, password er ligegyldigt ("x").
Alle quirks vi fandt i Make er bygget ind her (feltnavne, krav, sortering).
"""
import time
import requests
from .config import ORDRESTYRING_KEY, ORDRESTYRING_BASE

AUTH = (ORDRESTYRING_KEY, "x")
TIMEOUT = 30


def _req(method: str, path: str, params=None, json=None):
    url = f"{ORDRESTYRING_BASE}{path}"
    r = requests.request(method, url, auth=AUTH, params=params, json=json, timeout=TIMEOUT)
    if not r.ok:
        # Giv en læsbar fejl videre til agenten
        raise RuntimeError(f"ordrestyring {method} {path} -> {r.status_code}: {r.text}")
    try:
        return r.json()
    except ValueError:
        return {}


def _data(resp):
    """ordrestyring pakker svar i 'data' (liste eller objekt)."""
    return resp.get("data", resp) if isinstance(resp, dict) else resp


# ---------- Debtors (kunder) ----------

def search_debtors(*, name: str = None, address: str = None, postalcode: str = None, city: str = None):
    params = {}
    if name:
        params["customer_name"] = name
    if address:
        params["customer_address"] = address
    if postalcode:
        params["customer_postalcode"] = postalcode
    if city:
        params["customer_city"] = city
    return _data(_req("GET", "/debtors", params=params)) or []


def get_debtor(customer_number):
    return _data(_req("GET", f"/debtors/{customer_number}"))


# ---- Cache over ALLE kunder (til fuzzy-søgning, da API'et kun kan eksakt match) ----
_CACHE = {"rows": [], "ts": 0.0}
_CACHE_TTL = 900  # 15 min


def all_debtors(force=False):
    """Henter alle kunder (pagineret) og cacher dem i 15 min."""
    now = time.time()
    if not force and _CACHE["rows"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["rows"]
    rows, page = [], 1
    while page <= 60:  # sikkerhedsgrænse (60*100 = 6000 kunder)
        batch = _data(_req("GET", "/debtors", params={"page": page, "pagesize": 100})) or []
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    _CACHE["rows"], _CACHE["ts"] = rows, now
    return rows


def next_customer_number() -> int:
    """Højeste eksisterende kundenummer + 1 (ordrestyring auto-genererer ikke)."""
    resp = _req("GET", "/debtors", params={"sortby": "-customer_number", "page": 1, "pagesize": 1})
    rows = _data(resp) or []
    if not rows:
        return 1001
    try:
        return int(rows[0]["customer_number"]) + 1
    except (KeyError, ValueError, TypeError):
        return 1001


def create_debtor(*, navn, adresse, postnr, by, telefon="", email="",
                  mobil="", attention="", cvr=""):
    body = {
        "customer_number": next_customer_number(),
        "customer_name": navn,
        "customer_address": adresse,
        "customer_postalcode": postnr,
        "customer_city": by,
        "customer_telephone": telefon,
        "customer_email": email,
        "customer_mobile": mobil,
        "customer_attention": attention,
        "cvr": cvr,
        # Fakturaadresse = samme som kundeadresse (begge er påkrævede)
        "invoice_address": adresse,
        "invoice_postalcode": postnr,
        "invoice_city": by,
    }
    return _data(_req("POST", "/debtors", json=body))


def update_debtor(customer_number, **changes):
    """Henter eksisterende kunde, lægger ændringer oveni, gemmer (bevarer påkrævede felter)."""
    cur = get_debtor(customer_number)
    body = {
        "customer_number": customer_number,
        "customer_name": cur.get("customer_name"),
        "customer_address": cur.get("customer_address"),
        "customer_postalcode": cur.get("customer_postalcode"),
        "customer_city": cur.get("customer_city"),
        "invoice_address": cur.get("invoice_address"),
        "invoice_postalcode": cur.get("invoice_postalcode"),
        "invoice_city": cur.get("invoice_city"),
        "customer_telephone": cur.get("customer_telephone"),
        "customer_email": cur.get("customer_email"),
        "customer_mobile": cur.get("customer_mobile"),
        "cvr": cur.get("cvr"),
    }
    # Map agentens felt-navne -> API-felter, kun hvis udfyldt
    field_map = {
        "cvr": "cvr", "telefon": "customer_telephone", "email": "customer_email",
        "mobil": "customer_mobile", "adresse": "customer_address",
        "postnr": "customer_postalcode", "by": "customer_city",
    }
    for arg, val in changes.items():
        if val:
            body[field_map.get(arg, arg)] = val
    return _data(_req("PUT", f"/debtors/{customer_number}", json=body))


# ---------- Cases (sager) ----------

def get_cases():
    return _data(_req("GET", "/cases")) or []


def get_case(case_number):
    return _data(_req("GET", f"/cases/{case_number}"))


CASE_FIELD_MAP = {
    "beskrivelse": "description",
    "reference": "yourref",
    # kontaktperson/leveringsadresse er ID-baserede felter -> skrives som bemærkning i stedet
}


def _med_projekt(beskrivelse, projektnavn):
    """Projektnavn-feltet er ikke i API'et -> læg det forrest i beskrivelsen (synligt + søgbart)."""
    beskrivelse = beskrivelse or ""
    if projektnavn:
        return f"{projektnavn}: {beskrivelse}".rstrip(": ").strip()
    return beskrivelse


def create_case(*, customer_number, beskrivelse="", reference="", projektnavn=""):
    body = {"customer_number": customer_number, "description": _med_projekt(beskrivelse, projektnavn)}
    if reference:
        body["yourref"] = reference
    return _data(_req("POST", "/cases", json=body))


def update_case(case_number, **fields):
    """Opdater felter på en eksisterende sag. Tom værdi springes over."""
    body = {}
    for k, v in fields.items():
        if v not in (None, ""):
            body[CASE_FIELD_MAP.get(k, k)] = v
    if not body:
        return {}
    return _data(_req("PUT", f"/cases/{case_number}", json=body))


def add_remark(case_number, tekst, dato_str):
    """Tilføjer en bemærkning og bevarer historikken."""
    cur = get_case(case_number)
    gammel = (cur.get("remarks") or "").replace("\n", " | ")
    ny = f"{gammel} | [{dato_str} Aura] {tekst}".lstrip(" |")
    return _data(_req("PUT", f"/cases/{case_number}", json={"remarks": ny}))


def close_case(case_number, work_done=""):
    """Færdigmelder en sag (status + 'Færdiggjort arbejde'). Status-ID afhænger af firmaet."""
    body = {"work_done": work_done}
    # TODO: sæt 'status' til firmaets 'afsluttet'-status-id (hentes fra /case-statuses)
    return _data(_req("PUT", f"/cases/{case_number}", json=body))


# ---------- Invoices (fakturaer) ----------

def overdue_unpaid_invoices():
    """Forfaldne, ubetalte fakturaer (payed_date sættes via e-conomic-afstemning)."""
    import time
    resp = _req("GET", "/debtor-invoices", params={
        "type": 1, "payment_date-min": 1, "payment_date-max": int(time.time()),
    })
    rows = _data(resp) or []
    return [r for r in rows if not r.get("payed_date")]

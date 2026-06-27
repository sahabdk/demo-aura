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
    "kontaktperson": "contact",   # 'contact' er et fri-tekst-felt på sagen (string:100)
    # leveringsadresse er et ID-felt (delivery_address) -> håndteres via link_leveringsadresse()
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


# ---------- Nye ordrer + medarbejdere (til knap-menuen) ----------

def new_cases(since_ts):
    """Sager oprettet efter since_ts (unix). Nyeste først."""
    return _data(_req("GET", "/cases", params={
        "created_at-min": int(since_ts), "sortby": "-created_at", "pagesize": 100,
    })) or []


def today_cases():
    """Alle sager oprettet i dag (uanset om de er set). Nyeste først."""
    import datetime as _dt
    start = int(_dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    return _data(_req("GET", "/cases", params={
        "created_at-min": start, "sortby": "-created_at", "pagesize": 100,
    })) or []


_USERS = {"rows": [], "ts": 0.0}


def users(force=False):
    now = time.time()
    if not force and _USERS["rows"] and (now - _USERS["ts"]) < 1800:
        return _USERS["rows"]
    _USERS["rows"] = _data(_req("GET", "/users")) or []
    _USERS["ts"] = now
    return _USERS["rows"]


def user_name(user_id):
    for u in users():
        if str(u.get("id")) == str(user_id):
            nm = u.get("fullName") or f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()
            return nm or u.get("init")
    return None


def assign_case(case_number, technician_id):
    return _data(_req("PUT", f"/cases/{case_number}", json={"main_technician": int(technician_id)}))


# ---------- Leveringsadresser (delivery addresses) ----------

def delivery_addresses(customer_number):
    """Kundens gemte leveringsadresser. Tabellen 'cust_delivery_addresses' har ikke
    en 'customer_number'-kolonne at filtrere på, så vi henter siden og filtrerer i Python."""
    rows = _data(_req("GET", "/delivery-addresses", params={"pagesize": 100})) or []
    return [d for d in rows if str(d.get("customer_number")) == str(customer_number)]


def create_delivery_address(*, customer_number, adresse, postnr="", by="", navn="", att="", telefon="", email=""):
    body = {
        "customer_number": customer_number,
        "name": navn or "", "address": adresse, "postalcode": postnr, "city": by,
        "att": att, "telephone": telefon, "email": email,
    }
    return _data(_req("POST", "/delivery-addresses", json=body))


def _split_adresse(tekst):
    """Del fri tekst op i (adresse, postnr, by) ud fra et 4-cifret postnummer."""
    import re as _re
    tekst = (tekst or "").strip()
    m = _re.search(r"\b(\d{4})\b", tekst)
    if not m:
        return tekst, "", ""
    postnr = m.group(1)
    adresse = tekst[:m.start()].strip(" ,")
    by = tekst[m.end():].strip(" ,")
    return adresse or tekst, postnr, by


def link_leveringsadresse(case_number, customer_number, tekst):
    """Find en matchende leveringsadresse hos kunden ELLER opret en ny, og sæt
    dens id på sagens delivery_address-felt. Returnerer hvad der skete."""
    adresse, postnr, by = _split_adresse(tekst)
    da_id = None
    soeg = (adresse or tekst or "").lower()
    if soeg:
        try:  # match er best-effort: må aldrig forhindre oprettelse
            for da in delivery_addresses(customer_number):
                kandidat = f"{da.get('address','')} {da.get('postalcode','')} {da.get('city','')} {da.get('name','')}".lower()
                if soeg in kandidat:
                    da_id = da.get("id")
                    break
        except Exception:
            da_id = None
    oprettet = False
    if not da_id:
        ny = create_delivery_address(customer_number=customer_number, adresse=adresse, postnr=postnr, by=by)
        da_id = (ny or {}).get("id")
        oprettet = True
    if not da_id:
        raise RuntimeError("kunne ikke finde eller oprette leveringsadresse")
    _req("PUT", f"/cases/{case_number}", json={"delivery_address": int(da_id)})
    return {"id": da_id, "oprettet": oprettet, "adresse": tekst}


# ---------- Kontaktpersoner (debtor contacts) ----------

def debtor_contacts(customer_number):
    """Kundens kontaktpersoner (hentes som include på kunden). Hver har: id, name,
    email, telephone, mobile, address, postalcode, city."""
    d = _data(_req("GET", f"/debtors/{customer_number}", params={"include": "contacts"})) or {}
    cs = d.get("contacts")
    return cs if isinstance(cs, list) else []


def _debtor_med_kontakter(customer_number):
    """Henter kunden inkl. dens kontaktliste."""
    d = _data(_req("GET", f"/debtors/{customer_number}", params={"include": "contacts"})) or {}
    contacts = d.get("contacts")
    return d, (contacts if isinstance(contacts, list) else [])


_KONTAKT_FELTER = ("name", "email", "telephone", "mobile", "address", "postalcode", "city", "att", "ean")


def _rens_kontakt(c):
    """Behold kun de skrivbare tekstfelter og lav null -> "" (serverens preg_replace
    kan ikke håndtere null eller server-styrede felter som created_at/eco_*)."""
    ud = {}
    if c.get("id"):
        ud["id"] = c["id"]
    for k in _KONTAKT_FELTER:
        v = c.get(k)
        ud[k] = "" if v is None else v
    return ud


def _gem_kunde_kontakter(customer_number, d, contacts):
    """Gemmer kundens kontaktliste ved at sende den med i en opdatering af hele kunden
    (kontakt-underressourcen tillader ikke PUT/POST direkte). Påkrævede felter bevares."""
    body = {
        "customer_number": customer_number,
        "customer_name": d.get("customer_name"),
        "customer_address": d.get("customer_address"),
        "customer_postalcode": d.get("customer_postalcode"),
        "customer_city": d.get("customer_city"),
        "invoice_address": d.get("invoice_address"),
        "invoice_postalcode": d.get("invoice_postalcode"),
        "invoice_city": d.get("invoice_city"),
        "contacts": [_rens_kontakt(c) for c in contacts],
    }
    return _data(_req("PUT", f"/debtors/{customer_number}", json=body))


def link_kontaktperson(case_number, customer_number, navn):
    """Sæt kontaktperson på sagen så det rammer Kontaktperson-KORTET hvis muligt.

    Det kræver en kontakt der ALLEREDE findes på kunden (at oprette nye kontakter
    understøttes ikke af ordrestyrings v2-API). Findes navnet som kontakt -> link dens
    id til sagens contact-felt (kortet). Findes det ikke -> læg navnet i Rekvirenten,
    som er synligt på ordren. Returnerer hvilken metode der blev brugt."""
    navn_l = (navn or "").strip().lower()
    if not navn_l:
        raise RuntimeError("tomt kontaktperson-navn")
    match = None
    try:
        _, contacts = _debtor_med_kontakter(customer_number)
        match = next((c for c in contacts if (c.get("name") or "").strip().lower() == navn_l), None)
    except Exception:
        match = None
    cid = (match or {}).get("id")
    if cid:
        _req("PUT", f"/cases/{case_number}", json={"contact": str(cid)})
        return {"metode": "kort", "navn": navn}
    # Ingen eksisterende kontakt -> synligt fallback i Rekvirenten
    update_case(case_number, requestor=navn)
    return {"metode": "rekvirent", "navn": navn}

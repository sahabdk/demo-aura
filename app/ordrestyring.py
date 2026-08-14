"""Klient til ordrestyring.dk API v2.

HTTP Basic auth: API-nøglen er brugernavn, password er ligegyldigt ("x").
Alle quirks vi fandt i Make er bygget ind her (feltnavne, krav, sortering).
"""
import os
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
    def _side(p):
        return _data(_req("GET", "/debtors", params={"page": p, "pagesize": 100})) or []

    rows = _side(1)
    if len(rows) == 100:
        # hent resten i boelger a 8 sider parallelt (2400+ kunder: ~4 sek i stedet for ~30)
        from concurrent.futures import ThreadPoolExecutor
        naeste = 2
        with ThreadPoolExecutor(max_workers=8) as ex:
            while naeste <= 60:  # sikkerhedsgraense (60*100 = 6000 kunder)
                boelge = list(range(naeste, min(naeste + 8, 61)))
                faerdig = False
                for batch in ex.map(_side, boelge):
                    rows.extend(batch)
                    if len(batch) < 100:
                        faerdig = True
                if faerdig:
                    break
                naeste += 8
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


def saet_rekvirent(case_number, navn):
    """Rekvirent-feltet hedder 'requestor' i v2-API'et (bekraeftet via raa felt-dump
    paa sag 28710: baade v2, UpdateCaseInput og CreateCaseInput har 'requestor')."""
    _data(_req("PUT", f"/cases/{case_number}", json={"requestor": navn}))
    return {"felt": "requestor", "navn": navn}


def add_remark(case_number, tekst, dato_str):
    """Tilføjer en bemærkning og bevarer historikken."""
    cur = get_case(case_number)
    gammel = (cur.get("remarks") or "").replace("\n", " | ")
    ny = f"{gammel} | [{dato_str} Aura] {tekst}".lstrip(" |")
    return _data(_req("PUT", f"/cases/{case_number}", json={"remarks": ny}))


_STATUSES = {"rows": [], "ts": 0.0}
# Tekster der betyder "sagen er afsluttet" (firmaets statusnavne kan variere)
_LUKKE_ORD = ("afslut", "lukket", "luk", "færdig", "udført", "completed", "closed", "done")


def case_statuses(force=False):
    """Firmaets sags-statusser (id + tekst), cachet 30 min."""
    now = time.time()
    if not force and _STATUSES["rows"] and (now - _STATUSES["ts"]) < 1800:
        return _STATUSES["rows"]
    _STATUSES["rows"] = _data(_req("GET", "/case-statuses")) or []
    _STATUSES["ts"] = now
    return _STATUSES["rows"]


def closed_status_id():
    """Finder id'et for 'afsluttet'-status. Env CLOSE_STATUS_ID vinder (sættes ved levering
    for 100% sikkerhed); ellers gættes ud fra statusteksten."""
    env = os.environ.get("CLOSE_STATUS_ID")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    for s in case_statuses():
        tekst = (s.get("text") or "").lower()
        if any(o in tekst for o in _LUKKE_ORD):
            return s.get("id")
    return None


def close_case(case_number, work_done=""):
    """Færdigmelder en sag: sætter 'Færdiggjort arbejde' OG status til 'afsluttet' (hvis fundet).
    Returnerer hvilken status der blev sat (eller None hvis ingen kunne findes)."""
    sid = closed_status_id()
    body = {"work_done": work_done or ""}
    if sid is not None:
        body["status"] = sid
    _req("PUT", f"/cases/{case_number}", json=body)
    return {"status_id": sid}


def set_case_status(case_number, status_id):
    """Sæt sagens status (fx Igangværende) uden at røre andre felter."""
    return _req("PUT", f"/cases/{case_number}", json={"status": int(status_id)})


# ---------- Reference-scanning (kundeportal) ----------

def recent_cases(days=14, maks_sider=30):
    """Sager oprettet inden for de seneste 'days' dage (på tværs af kunder).
    /cases kan IKKE filtreres på customer_number (giver 500), så vi henter seneste sager
    via created_at-filteret og filtrerer pr. kunde i Python."""
    import datetime as _dt
    start = int((_dt.datetime.now() - _dt.timedelta(days=days)).timestamp())
    rows, page = [], 1
    while page <= maks_sider:
        batch = _data(_req("GET", "/cases", params={
            "created_at-min": start, "sortby": "-created_at", "page": page, "pagesize": 100,
        })) or []
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return rows


_CASES_CACHE = {"rows": None, "sider": 0, "ts": 0.0}
_HOURS_CACHE = {"rows": None, "ts": 0.0}


def ryd_kortcache():
    """Ryd korttids-cachen (kaldes efter enhver ændring, så svar aldrig er forældede)."""
    _CASES_CACHE["rows"] = None
    _HOURS_CACHE["rows"] = None


def cases_paged(maks_sider=5):
    """Seneste sager (pagineret, nyeste først). Korttids-cache (45 sek) — flere opslag i
    samme svar rammer så kun ordrestyring én gang. Ryddes ved ændringer (ryd_kortcache)."""
    now = time.time()
    if (_CASES_CACHE["rows"] is not None and _CASES_CACHE["sider"] >= maks_sider
            and now - _CASES_CACHE["ts"] < 45):
        return _CASES_CACHE["rows"]
    rows, page = [], 1
    while page <= maks_sider:
        batch = _data(_req("GET", "/cases", params={
            "sortby": "-created_at", "page": page, "pagesize": 100,
        })) or []
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    _CASES_CACHE.update(rows=rows, sider=maks_sider, ts=time.time())
    return rows


def has_reference(case):
    return bool((case.get("yourref") or "").strip())


def is_closed(case):
    sid = closed_status_id()
    return sid is not None and str(case.get("status")) == str(sid)


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


# Hos Vandt & Vandt: medarbejder-status 999912 = INAKTIV, 999913 = aktiv
# (bekraeftet mod ordrestyring-web 14/8-26). Kan overstyres pr. kunde via env.
_USER_INAKTIV_STATUS = os.environ.get("USER_INACTIVE_STATUS", "999912")


def _bruger_er_aktiv(u):
    """Kun AKTIVE medarbejdere skal kunne vaelges (menu, tildeling, opslag).
    Vi frasorterer paa INAKTIV-status (fail-open: ukendte statusser vises)."""
    if str(u.get("status")) == str(_USER_INAKTIV_STATUS):
        return False
    for k, v in u.items():
        kl = str(k).lower()
        if kl in ("active", "is_active", "enabled") and v in (0, False, "0", "false"):
            return False
        if kl in ("disabled", "deleted", "deactivated", "archived", "inactive",
                  "is_deleted", "hidden") and v in (1, True, "1", "true"):
            return False
    return True


def users(force=False, alle=False):
    now = time.time()
    if force or not _USERS["rows"] or (now - _USERS["ts"]) >= 1800:
        _USERS["rows"] = _data(_req("GET", "/users")) or []
        _USERS["ts"] = now
    rows = _USERS["rows"]
    return rows if alle else [u for u in rows if _bruger_er_aktiv(u)]


def user_name(user_id):
    for u in users():
        if str(u.get("id")) == str(user_id):
            nm = u.get("fullName") or f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()
            return nm or u.get("init")
    return None


def assign_case(case_number, technician_id):
    return _data(_req("PUT", f"/cases/{case_number}", json={"main_technician": int(technician_id)}))


def faktura_email(debtor):
    """Mail til fakturapost (rykkere, referenceanmodninger): FAKTURERINGS-mailen
    fra kundekortet har 1. prioritet, ellers kundens almindelige email."""
    for k, v in (debtor or {}).items():
        kl = str(k).lower()
        if ("invoice" in kl or "faktura" in kl) and "mail" in kl and v and "@" in str(v):
            return str(v).strip()
    e = ((debtor or {}).get("customer_email") or "").strip()
    return e or None


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
        import difflib
        _, contacts = _debtor_med_kontakter(customer_number)
        # Fleksibel match på kundens egne kontakter: præcis -> delvis -> ~tastefejl
        best = (0.0, None)
        for c in contacts:
            cn = (c.get("name") or "").strip().lower()
            if not cn:
                continue
            if cn == navn_l or navn_l in cn or cn in navn_l:
                match = c
                break
            r = difflib.SequenceMatcher(None, navn_l, cn).ratio()
            if r > best[0]:
                best = (r, c)
        if not match and best[1] and best[0] >= 0.8:
            match = best[1]
    except Exception:
        match = None
    cid = (match or {}).get("id")
    if cid:
        _req("PUT", f"/cases/{case_number}", json={"contact": str(cid)})
        return {"metode": "kort", "navn": navn}
    # Ingen eksisterende kontakt -> synligt fallback i Rekvirenten
    update_case(case_number, requestor=navn)
    return {"metode": "rekvirent", "navn": navn}


# ---------- Timer (hours / timeregistrering på en sag) ----------

_EMP_TYPES = {"rows": [], "ts": 0.0}


def employee_types(force=False):
    """Firmaets time-typer (Employee types: id + title), cachet 30 min. Bruges som 'Type'."""
    now = time.time()
    if not force and _EMP_TYPES["rows"] and (now - _EMP_TYPES["ts"]) < 1800:
        return _EMP_TYPES["rows"]
    _EMP_TYPES["rows"] = _data(_req("GET", "/employee-types")) or []
    _EMP_TYPES["ts"] = now
    return _EMP_TYPES["rows"]


def find_hour_type(navn=None):
    """Find en time-type-id ud fra navn (fx 'normal', 'overtid'); ellers en fornuftig standard."""
    typer = employee_types()
    if not typer:
        return None
    if navn:
        nl = str(navn).strip().lower()
        for t in typer:
            if (t.get("title") or "").strip().lower() == nl:
                return t.get("id")
        for t in typer:
            if nl and nl in (t.get("title") or "").strip().lower():
                return t.get("id")
    # standard: foretræk en almindelig arbejds-type, ellers den første
    for t in typer:
        if any(o in (t.get("title") or "").lower() for o in ("normal", "arbejde", "alm", "standard", "time")):
            return t.get("id")
    return typer[0].get("id")


def hours_raw():
    """Raa timelinjer fra /hours (korttids-cache 30 sek; ryddes ved ændringer)."""
    now = time.time()
    if _HOURS_CACHE["rows"] is not None and now - _HOURS_CACHE["ts"] < 30:
        return _HOURS_CACHE["rows"]
    rows = _data(_req("GET", "/hours")) or []
    _HOURS_CACHE.update(rows=rows, ts=now)
    return rows


def register_hours(*, case_id, emp_id, start_time, stop_time, hour_type, remark="", case_number=None):
    """Opret en timelinje på en sag. Tider er unix-sekunder.

    GET /hours viser at systemet selv gemmer 'new_case_number' (sagsnummer, IKKE internt id)
    og 'approval_status'. Vi prøver derfor flere felt-varianter og logger hver afvisning."""
    base = {
        "emp_id": int(emp_id),
        "start_time": int(start_time),
        "stop_time": int(stop_time),
        "hour_type": int(hour_type),
    }
    if remark:
        base["remark"] = remark
    varianter = []
    if case_number:
        varianter.append({**base, "new_case_number": str(case_number), "approval_status": 1})
        varianter.append({**base, "new_case_number": str(case_number)})
    varianter.append({**base, "case_id": int(case_id), "approval_status": 1})
    varianter.append({**base, "case_id": int(case_id)})
    sidste = None
    for i, body in enumerate(varianter, 1):
        try:
            res = _data(_req("POST", "/hours", json=body))
            print(f"[register_hours] variant {i} ({sorted(body.keys())}) VIRKEDE", flush=True)
            return res
        except Exception as e:
            sidste = e
            print(f"[register_hours] variant {i} ({sorted(body.keys())}) fejlede: {str(e)[:200]}", flush=True)
    raise sidste

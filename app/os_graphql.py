"""Klient til ordrestyrings GraphQL-API: varesøgning + tilføj materiale til en sag.

v2-REST-API'et kan ikke søge i vare-kataloget, så det gør vi via GraphQL
(graphql.ordrestyring.dk). Samme API-nøgle som v2, sendt som Bearer-token.
"""
import requests
from .config import OS_GRAPHQL_URL, OS_GRAPHQL_KEY

TIMEOUT = 25


def _gql(query: str, variables=None):
    r = requests.post(
        OS_GRAPHQL_URL,
        headers={"Authorization": f"Bearer {OS_GRAPHQL_KEY}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=TIMEOUT,
    )
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"GraphQL svarede ikke JSON ({r.status_code})")
    if data.get("errors"):
        import json as _json
        e0 = data["errors"][0]
        besked = str(e0.get("message", "GraphQL-fejl"))
        ekstra = e0.get("extensions")
        if ekstra:   # fx valideringsdetaljer: hvilket felt og hvorfor
            besked += " | " + _json.dumps(ekstra, ensure_ascii=False)[:400]
        raise RuntimeError(besked[:600])
    return data.get("data") or {}


def _q(s):
    """Escape til inline-strenge i en GraphQL-query."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


# ---------- varesøgning ----------

def _pris_kr(suppliers):
    """Vælg en pris (kr) fra leverandør-listen. Priser er i øre."""
    for s in suppliers or []:
        v = s.get("salesPrice") or s.get("listPrice")
        if v:
            try:
                return round(float(v) / 100.0, 2)
            except (ValueError, TypeError):
                pass
    return None


def _supplier_products(term, limit):
    """Leverandør-katalog (kræver klarpris-adgang på API-nøglen)."""
    q = f'''{{
      supplierProducts(search: "{_q(term)}", pagination: {{cursor: null, limit: {int(limit) + 1}}},
                       searchOrigin: OFFER) {{
        identifier description number eanNumber
        suppliers {{ salesPrice listPrice displayName }}
      }}
    }}'''
    rows = _gql(q).get("supplierProducts") or []
    return [{
        "id": r.get("identifier"), "number": r.get("number"), "ean": r.get("eanNumber"),
        "description": r.get("description"), "price_kr": _pris_kr(r.get("suppliers")),
    } for r in rows]


def _own_products(term, limit):
    """Firmaets egne varer (products-kataloget)."""
    q = f'''{{
      products(pagination: {{cursor: null, limit: {int(limit) + 1}}},
               search: {{query: "{_q(term)}", fields: ["description", "number"]}}) {{
        items {{ id number description listPrice }}
      }}
    }}'''
    items = (_gql(q).get("products") or {}).get("items") or []
    out = []
    for p in items:
        try:
            # listPrice er i øre (fx 15000 = 150,00 kr) — samme som leverandør-priser
            pris = round(float(p.get("listPrice")) / 100.0, 2) if p.get("listPrice") else None
        except (ValueError, TypeError):
            pris = None
        out.append({"id": p.get("id"), "number": p.get("number"), "ean": p.get("number"),
                    "description": p.get("description"), "price_kr": pris})
    return out


def search_products(term: str, limit: int = 5):
    """Søg vare-katalog. Prøver leverandør-kataloget først, ellers firmaets egne varer.
    Returnerer [{id, number, ean, description, price_kr}] + hvor mange flere der er."""
    out = []
    try:
        out = _supplier_products(term, limit)
    except Exception:
        out = []
    if not out:
        try:
            out = _own_products(term, limit)
        except Exception:
            out = []
    flere = max(0, len(out) - int(limit))
    return out[:int(limit)], flere


def product_by_ean(ean: str):
    """Find præcis vare ud fra stregkode/EAN-nummer."""
    items, _ = search_products(str(ean), limit=10)
    e = str(ean).strip()
    for p in items:
        if str(p.get("ean") or "").strip() == e or str(p.get("number") or "").strip() == e:
            return p
    return items[0] if items else None


# ---------- tilføj materiale til en sag ----------

def _case_internal_id(case_number):
    q = f'{{ caseByCaseNumber(caseNumber: "{_q(str(case_number))}") {{ id caseNumber }} }}'
    c = _gql(q).get("caseByCaseNumber") or {}
    return c.get("id")


def add_case_material(case_number, identifier=None, quantity=1,
                      product_number=None, description=None):
    """Læg en vare på en sag. identifier = varens GraphQL-id (linker til kataloget)."""
    cid = _case_internal_id(case_number)
    if not cid:
        raise RuntimeError(f"kunne ikke finde sag {case_number}")
    felter = [f"caseId: {int(cid)}", f"quantity: {float(quantity)}"]
    ident = str(identifier) if identifier is not None else ""
    if "." in ident:      # leverandør-vare: identifier-token bærer varenummer + priser
        felter.append(f'identifier: "{_q(ident)}"')
    else:                 # egen/manuel vare: send varenummer + beskrivelse direkte
        if product_number:
            felter.append(f'productNumber: "{_q(str(product_number))}"')
        if description:
            felter.append(f'description: "{_q(description)}"')
    q = f'mutation {{ createCaseMaterial(input: {{{", ".join(felter)}}}) {{ id }} }}'
    return _gql(q).get("createCaseMaterial") or {}


# ---------- DEBUG: findes der timer-mutationer i GraphQL? ----------

def find_hour_fields(words=("hour", "time", "tid")):
    """Introspektér GraphQL-skemaet og find alle queries/mutationer der matcher
    soegeordene (default: timer-relaterede). Bruges til at udforske API'et."""
    q = "{ __schema { mutationType { fields { name } } queryType { fields { name } } } }"
    data = _gql(q)

    def _names(t):
        return [f.get("name") or "" for f in (((data.get("__schema") or {}).get(t) or {}).get("fields") or [])]

    alle = {"mutations": _names("mutationType"), "queries": _names("queryType")}
    hits = {k: [x for x in v if any(w in x.lower() for w in words)]
            for k, v in alle.items()}
    return hits, alle


def describe_type(name):
    """Introspektér en vilkaarlig GraphQL-type: felter/inputfelter med typer."""
    q = ('{ __type(name: "' + _q(name) + '") { kind name '
         "fields { name type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } } "
         "inputFields { name type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } } "
         "} }")
    t = _gql(q).get("__type") or {}
    felter = {}
    for f in (t.get("fields") or []) + (t.get("inputFields") or []):
        felter[f.get("name")] = _ts(f.get("type"))
    return t.get("kind"), felter


def _ts(t):
    """GraphQL-type -> laesbar streng, fx Int!, [String], CreateHourInput!"""
    if not t:
        return "?"
    k = t.get("kind")
    if k == "NON_NULL":
        return _ts(t.get("ofType")) + "!"
    if k == "LIST":
        return "[" + _ts(t.get("ofType")) + "]"
    return t.get("name") or "?"


def describe_hour_input():
    """Bagudkompatibel wrapper: createHour-mutationens argumenter + input-felter."""
    return describe_mutation("createHour")


def describe_mutation(name):
    """Introspektér en vilkaarlig mutations argumenter + inputtypernes felter."""
    q1 = ("{ __schema { mutationType { fields { name args { name "
          "type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } "
          "} } } } }")
    data = _gql(q1)
    fields = (((data.get("__schema") or {}).get("mutationType") or {}).get("fields")) or []
    fld = next((f for f in fields if f.get("name") == name), None)
    if not fld:
        raise RuntimeError(f"{name} findes ikke i skemaet")
    args = {a.get("name"): _ts(a.get("type")) for a in (fld.get("args") or [])}
    detaljer = {}
    for ts in args.values():
        tn = ts.replace("!", "").replace("[", "").replace("]", "")
        q2 = ('{ __type(name: "' + tn + '") { kind inputFields { name '
              "type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } "
              "} } }")
        try:
            t = _gql(q2).get("__type") or {}
        except Exception:
            continue
        if t.get("inputFields"):
            detaljer[tn] = {f.get("name"): _ts(f.get("type")) for f in t["inputFields"]}
    # gaa et niveau dybere: nested input-typer (fx PauseInput, HourAdditionInput)
    SKALARER = {"Int", "String", "Boolean", "Float", "ID", "Upload"}
    nested = set()
    for felter in list(detaljer.values()):
        for ft in felter.values():
            tn = ft.replace("!", "").replace("[", "").replace("]", "")
            if tn not in SKALARER and tn not in detaljer:
                nested.add(tn)
    for tn in nested:
        q3 = ('{ __type(name: "' + tn + '") { kind inputFields { name '
              "type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } "
              "} } }")
        try:
            t = _gql(q3).get("__type") or {}
        except Exception:
            continue
        if t.get("inputFields"):
            detaljer[tn] = {f.get("name"): _ts(f.get("type")) for f in t["inputFields"]}
    return args, detaljer


# ---------- timeregistrering via GraphQL (v2 POST /hours er blokeret, se overlevering §8) ----------

def pause_types():
    """Firmaets pause-typer (fx Frokost, 30 min): [{id, name, minutes}].
    'pauses' er pagineret (PausePagination) og kraever pagination-argumentet."""
    q = "{ pauses(pagination: {cursor: null, limit: 50}) { items { id name minutes } } }"
    rows = (_gql(q).get("pauses") or {}).get("items") or []
    print(f"[pause_types] {rows}", flush=True)
    return rows


def create_hour(*, case_id, user_id, hour_type_id, start_time, stop_time,
                description=None, pauses=None):
    """Opret en timelinje via GraphQL createHour. Tider er unix-sekunder.
    CreateHourInput: caseId Int, userId Int!, hourTypeId Int!, startTime Int!,
    stopTime Int!, description String, pauses [PauseInput: pauseTypeId+quantity]."""
    felter = [
        f"userId: {int(user_id)}",
        f"hourTypeId: {int(hour_type_id)}",
        f"startTime: {int(start_time)}",
        f"stopTime: {int(stop_time)}",
    ]
    if case_id:
        felter.insert(0, f"caseId: {int(case_id)}")
    if description:
        felter.append(f'description: "{_q(description)}"')
    if pauses:
        p = ", ".join("{pauseTypeId: %d, quantity: %d}" % (int(x["pauseTypeId"]), int(x["quantity"]))
                      for x in pauses)
        felter.append(f"pauses: [{p}]")
    q = f'mutation {{ createHour(input: {{{", ".join(felter)}}}) {{ id }} }}'
    return _gql(q).get("createHour") or {}


def _query_args(name):
    """Introspektér en query's argumenter: {navn: type-streng}."""
    q1 = ("{ __schema { queryType { fields { name args { name "
          "type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } "
          "} } } } }")
    data = _gql(q1)
    fields = (((data.get("__schema") or {}).get("queryType") or {}).get("fields")) or []
    fld = next((f for f in fields if f.get("name") == name), None)
    if not fld:
        raise RuntimeError(f"{name} findes ikke i skemaet")
    return {a.get("name"): _ts(a.get("type")) for a in (fld.get("args") or [])}


def documentation_count(case_number):
    """Antal filer i sagens DOKUMENTATION-fane (adskilt fra 'Dokumenter'/documentCount).
    Bygger kaldet helt selv-opdagende: enum-argumenter slaas op (CASE-varianten vaelges),
    refId = sagens interne id, og retur-objektets skalar-felter selekteres automatisk."""
    cid = _case_internal_id(case_number)
    if not cid:
        return None
    SKALARER = ("Int", "String", "Boolean", "Float", "ID")
    args = _query_args("documentationFileCount")
    dele = []
    for navn, ts in args.items():
        tn = ts.replace("!", "")
        kraevet = ts.endswith("!")
        if tn in SKALARER:
            if kraevet:
                dele.append(f'{navn}: "{cid}"' if tn in ("String", "ID") else f"{navn}: {int(cid)}")
        else:
            vals = _enum_values(tn)
            if vals:
                v = next((x for x in vals if "CASE" in x.upper()), vals[0])
                dele.append(f"{navn}: {v}")
    sel = ""
    try:
        _, retfelter = describe_type("DocumentationFolderFileCount")
        felter = [k for k, v in retfelter.items() if v.replace("!", "") in SKALARER]
        if felter:
            sel = " { " + " ".join(felter) + " }"
    except Exception:
        pass
    q = f"{{ documentationFileCount({', '.join(dele)}){sel} }}"
    print(f"[documentation_count] {q[:250]}", flush=True)
    res = _gql(q).get("documentationFileCount")
    print(f"[documentation_count] resultat={res}", flush=True)
    if isinstance(res, dict):
        for k, v in res.items():   # foretraek et *count*-felt, ellers foerste heltal
            if "count" in k.lower() and isinstance(v, int):
                return v
        return next((v for v in res.values() if isinstance(v, int)), None)
    return res


# ---------- medarbejdere paa sagen + planlagt tid ----------

def case_user_ids(case_number):
    """Sagens interne id + listen af medarbejder-id'er (Medarbejdere-kortet)."""
    q = f'{{ caseByCaseNumber(caseNumber: "{_q(str(case_number))}") {{ id users {{ id }} }} }}'
    c = _gql(q).get("caseByCaseNumber") or {}
    return c.get("id"), [u.get("id") for u in (c.get("users") or []) if u.get("id") is not None]


def add_case_user(case_number, user_id):
    """Tilfoej en medarbejder til sagens Medarbejdere-liste (bevarer de eksisterende)."""
    cid, ids = case_user_ids(case_number)
    if not cid:
        raise RuntimeError(f"kunne ikke finde sag {case_number}")
    ny = sorted({int(i) for i in ids} | {int(user_id)})
    q = "mutation($input: UpdateCaseInput!) { updateCase(id: %d, input: $input) { id } }" % int(cid)
    return _gql(q, {"input": {"userIds": ny}}).get("updateCase") or {}


def create_planned_event(case_number, user_ids, start_time, stop_time, text=None):
    """Opret Planlagt tid paa en sag (vises i Dagsoversigt/kalender).
    addUsersToCase=True laegger samtidig medarbejderne paa sagens Medarbejdere-liste."""
    cid = _case_internal_id(case_number)
    if not cid:
        raise RuntimeError(f"kunne ikke finde sag {case_number}")
    typer = _enum_values("EventType")
    etype = (next((v for v in typer if "PLAN" in v.upper()), None)
             or next((v for v in typer if "CASE" in v.upper() or "SAG" in v.upper()), None)
             or (typer[0] if typer else "PLANNING"))
    print(f"[create_planned_event] EventType={typer} -> {etype}", flush=True)
    inp = {"type": etype, "userIds": [int(u) for u in user_ids],
           "startTime": int(start_time), "stopTime": int(stop_time),
           "caseId": int(cid), "addUsersToCase": True}
    if text:
        inp["text"] = text
    q = "mutation($input: CreateEventInput!) { createEvent(input: $input) { id } }"
    try:
        res = _gql(q, {"input": inp}).get("createEvent")
    except RuntimeError as e:
        if "selection" in str(e).lower() or "must have" in str(e).lower():
            q2 = "mutation($input: CreateEventInput!) { createEvent(input: $input) }"
            res = _gql(q2, {"input": inp}).get("createEvent")
        else:
            raise
    if isinstance(res, list):   # createEvent returnerer en LISTE (en begivenhed pr. medarbejder)
        res = res[0] if res else {}
    return res or {}


# ---------- fortryd: slet objekter Aura selv har oprettet ----------

def _delete(mutation, obj_id):
    """Kør en delete-mutation robust (med/uden selektion alt efter retur-typen)."""
    q = f"mutation {{ {mutation}(id: {int(obj_id)}) }}"
    try:
        return _gql(q)
    except RuntimeError as e:
        if "selection" in str(e).lower() or "must have" in str(e).lower():
            return _gql(f"mutation {{ {mutation}(id: {int(obj_id)}) {{ id }} }}")
        raise


def delete_hour(hour_id):
    return _delete("deleteHour", hour_id)


def delete_case_material(material_id):
    return _delete("deleteCaseMaterial", material_id)


def delete_documentation_file(file_id):
    return _delete("deleteDocumentationFile", file_id)


def delete_event(event_id):
    """deleteEvent kraever ogsaa type: EventType! (samme enum som createEvent)."""
    typer = _enum_values("EventType")
    etype = (next((v for v in typer if "PLAN" in v.upper()), None)
             or (typer[0] if typer else "PLANNING"))
    q = f"mutation {{ deleteEvent(type: {etype}, id: {int(event_id)}) }}"
    try:
        return _gql(q)
    except RuntimeError as e:
        if "selection" in str(e).lower() or "must have" in str(e).lower():
            return _gql(f"mutation {{ deleteEvent(type: {etype}, id: {int(event_id)}) {{ id }} }}")
        raise


# ---------- samlet sag-overblik (til sag_status-vaerktoejet) ----------

def case_overview(case_number):
    """Skalar-felter + taellere fra sagen i ET opslag (dokumenter, timer, fakturaer m.m.)."""
    q = f'''{{ caseByCaseNumber(caseNumber: "{_q(str(case_number))}") {{
      id caseNumber description remarks workDone reference requisition orderNumber projectName
      createdAt updatedAt documentCount hoursCount salesInvoicesCount schemesCount
    }} }}'''
    return _gql(q).get("caseByCaseNumber") or {}


def planned_events(case_number):
    """Planlagte tider for en sag (Dagsoversigt/kalender): [{id, startTime, stopTime, text}]."""
    cid = _case_internal_id(case_number)
    if not cid:
        return []
    q = (f"{{ plannedEvents(caseId: {int(cid)}) "
         "{ items { id startTime stopTime text user { id } } } }")
    try:
        res = _gql(q).get("plannedEvents")
    except RuntimeError as e:
        # nogle paginerede typer kraever pagination-argumentet alligevel
        if "pagination" in str(e).lower():
            q2 = (f"{{ plannedEvents(caseId: {int(cid)}, pagination: {{cursor: null, limit: 50}}) "
                  "{ items { id startTime stopTime text user { id } } } }")
            res = _gql(q2).get("plannedEvents")
        else:
            raise
    if isinstance(res, dict):
        return res.get("items") or []
    return res or []


def planned_events_between(start_ts, stop_ts, user_id=None, maks_sager=200):
    """Planlagte tider (Dagsoversigt) i et tidsrum paa tvaers af aabne sager.
    Returnerer [{sagsnummer, beskrivelse, kunde, startTime, stopTime, user_id}].
    Scanner de aabne sager en for en (API'et kan ikke filtrere plannedEvents paa dato)."""
    from . import ordrestyring as os_api
    import time as _t
    _n = (int(start_ts), int(stop_ts))
    if _PLAN_CACHE.get("n") == _n and _t.time() - _PLAN_CACHE.get("ts", 0) < 180:
        alle = _PLAN_CACHE["rows"]
        return [x for x in alle if user_id is None or str(x.get("user_id")) == str(user_id)]
    ud = []
    kunder = {}
    try:
        kunder = {str(d.get("customer_number")): d.get("customer_name")
                  for d in os_api.all_debtors()}
    except Exception:
        pass
    # Statusser der IKKE er arbejde: lukket/afsluttet, aflyst, annulleret, faktureret
    doede = set()
    try:
        for s in os_api.case_statuses():
            t = (s.get("text") or "").lower()
            if any(o in t for o in ("lukket", "afslut", "aflyst", "annull", "faktur", "closed", "cancel")):
                doede.add(str(s.get("id")))
    except Exception:
        pass
    n = 0
    for c in os_api.cases_paged():
        if os_api.is_closed(c) or str(c.get("status")) in doede:
            continue
        n += 1
        if n > maks_sager:
            break
        nr = c.get("case_number")
        try:
            evts = planned_events(nr)
        except Exception:
            continue
        for e in evts:
            st, sp = int(e.get("startTime") or 0), int(e.get("stopTime") or 0)
            if not st or st > stop_ts or (sp or st) < start_ts:
                continue
            uid = (e.get("user") or {}).get("id")
            ud.append({"sagsnummer": nr,
                       "beskrivelse": (c.get("description") or "").strip()[:80],
                       "kunde": kunder.get(str(c.get("customer_number"))) or c.get("customer_number"),
                       "startTime": st, "stopTime": sp, "user_id": uid})
    ud.sort(key=lambda x: x["startTime"])
    _PLAN_CACHE.update({"n": _n, "ts": _t.time(), "rows": ud})
    return [x for x in ud if user_id is None or str(x.get("user_id")) == str(user_id)]


_PLAN_CACHE = {}


# ---------- dokumentation: upload foto/fil til en sags Dokumentation-fane ----------

def _enum_values(name):
    """Vaerdierne i en GraphQL-enum (fx DocumentationType)."""
    q = '{ __type(name: "' + _q(name) + '") { enumValues { name } } }'
    t = _gql(q).get("__type") or {}
    return [v.get("name") for v in (t.get("enumValues") or []) if v.get("name")]


def upload_case_document(case_number, filename, data_bytes, description=None):
    """Upload en fil (fx et foto) til sagens Dokumentation via base64.
    Enum-vaerdierne (type/provider) slaas op ved koersel og logges, saa vi ser dem i Railway."""
    import base64
    cid = _case_internal_id(case_number)
    if not cid:
        raise RuntimeError(f"kunne ikke finde sag {case_number}")
    typer = _enum_values("DocumentationType")
    dtype = next((v for v in typer if "CASE" in v.upper()), typer[0] if typer else "CASE")
    provs = _enum_values("DocumentationProvider")
    prov = next((v for v in provs if any(w in v.upper() for w in
                 ("INTERN", "ORDRE", "DEFAULT", "LOCAL", "STANDARD"))),
                provs[0] if provs else "INTERNAL")
    print(f"[upload_case_document] DocumentationType={typer} -> {dtype}; "
          f"DocumentationProvider={provs} -> {prov}; typeId={cid}, fil={filename}, "
          f"{len(data_bytes)} bytes", flush=True)
    variables = {"input": {
        "typeId": str(cid),
        "base64": {"filename": filename, "content": base64.b64encode(data_bytes).decode("ascii")},
    }}
    if description:
        variables["input"]["description"] = description
    q = ("mutation($input: UploadDocumentationFileInput!) { "
         f"uploadDocumentationFile(type: {dtype}, provider: {prov}, input: $input) {{ id }} }}")
    try:
        return _gql(q, variables).get("uploadDocumentationFile") or {}
    except RuntimeError as e:
        # Hvis retur-typen ikke har 'id' (eller er en skalar), proev uden selektion
        if "id" in str(e).lower() or "selection" in str(e).lower():
            q2 = ("mutation($input: UploadDocumentationFileInput!) { "
                  f"uploadDocumentationFile(type: {dtype}, provider: {prov}, input: $input) }}")
            return _gql(q2, variables).get("uploadDocumentationFile") or {}
        raise


# ---------- kontaktperson-kort (createContactPerson + link til sag) ----------

def _case_and_customer_ids(case_number):
    """Sagens interne id + kundens interne id (til createContactPerson)."""
    q = f'{{ caseByCaseNumber(caseNumber: "{_q(str(case_number))}") {{ id customer {{ id }} }} }}'
    c = _gql(q).get("caseByCaseNumber") or {}
    return c.get("id"), (c.get("customer") or {}).get("id")


def create_contact_person(customer_internal_id, name, email=None, phone=None):
    felter = [f'name: "{_q(name)}"', f"customerId: {int(customer_internal_id)}"]
    if email:
        felter.append(f'email: "{_q(email)}"')
    if phone:
        felter.append(f'phoneNumber: "{_q(phone)}"')
    q = f'mutation {{ createContactPerson(input: {{{", ".join(felter)}}}) {{ id name }} }}'
    return _gql(q).get("createContactPerson") or {}


def set_case_contact_person(case_number, customer_number, name, email=None, phone=None):
    """Find eller OPRET en kontakt på kunden og sæt den i sagens Kontaktperson-kort.
    Returnerer {'navn', 'oprettet'}."""
    from . import ordrestyring as os_api
    navn_l = (name or "").strip().lower()
    if not navn_l:
        raise RuntimeError("tomt kontaktperson-navn")

    # 1) findes kontakten allerede på kunden? (v2 include=contacts)
    contact_id = None
    if customer_number:
        try:
            _, contacts = os_api._debtor_med_kontakter(customer_number)
            contact_id = next((c.get("id") for c in contacts
                               if (c.get("name") or "").strip().lower() == navn_l), None)
        except Exception:
            contact_id = None

    oprettet = False
    if not contact_id:
        # 2) opret kontakten på kunden (kræver kundens interne id fra sagen)
        _, customer_internal_id = _case_and_customer_ids(case_number)
        if not customer_internal_id:
            raise RuntimeError("kunne ikke finde kundens interne id fra sagen")
        ny = create_contact_person(customer_internal_id, name, email, phone)
        contact_id = ny.get("id")
        oprettet = True
    if not contact_id:
        raise RuntimeError("kunne ikke oprette kontaktperson")

    # 3) sæt kontaktens id i sagens Kontaktperson-kort (v2 contact-felt)
    os_api.update_case(case_number, kontaktperson=str(contact_id))
    return {"navn": name, "oprettet": oprettet}

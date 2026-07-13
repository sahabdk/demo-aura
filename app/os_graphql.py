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
        raise RuntimeError(str(data["errors"][0].get("message", "GraphQL-fejl"))[:200])
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

def find_hour_fields():
    """Introspektér GraphQL-skemaet og find alle queries/mutationer der ligner timer
    (hour/time/tid i navnet). Bruges til at afgøre om timeregistrering kan gå via
    GraphQL i stedet for det blokerede v2 POST /hours."""
    q = "{ __schema { mutationType { fields { name } } queryType { fields { name } } } }"
    data = _gql(q)

    def _names(t):
        return [f.get("name") or "" for f in (((data.get("__schema") or {}).get(t) or {}).get("fields") or [])]

    alle = {"mutations": _names("mutationType"), "queries": _names("queryType")}
    hits = {k: [x for x in v if any(w in x.lower() for w in ("hour", "time", "tid"))]
            for k, v in alle.items()}
    return hits, alle


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
    """Introspektér createHour-mutationens argumenter + inputtypens felter,
    saa vi ved praecis hvad en GraphQL-timeregistrering skal indeholde."""
    q1 = ("{ __schema { mutationType { fields { name args { name "
          "type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } "
          "} } } } }")
    data = _gql(q1)
    fields = (((data.get("__schema") or {}).get("mutationType") or {}).get("fields")) or []
    fld = next((f for f in fields if f.get("name") == "createHour"), None)
    if not fld:
        raise RuntimeError("createHour findes ikke i skemaet")
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
    return args, detaljer


# ---------- timeregistrering via GraphQL (v2 POST /hours er blokeret, se overlevering §8) ----------

def create_hour(*, case_id, user_id, hour_type_id, start_time, stop_time, description=None):
    """Opret en timelinje via GraphQL createHour. Tider er unix-sekunder.
    CreateHourInput: caseId Int, userId Int!, hourTypeId Int!, startTime Int!,
    stopTime Int!, description String (+ pauses/additions, som vi ikke bruger endnu)."""
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
    q = f'mutation {{ createHour(input: {{{", ".join(felter)}}}) {{ id }} }}'
    return _gql(q).get("createHour") or {}


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

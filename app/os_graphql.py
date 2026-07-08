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
            pris = round(float(p.get("listPrice")), 2) if p.get("listPrice") else None
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

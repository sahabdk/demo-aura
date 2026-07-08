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

def search_products(term: str, limit: int = 5):
    """Søg i vare-kataloget. Returnerer op til 'limit' varer + evt. flere."""
    q = f'''{{
      products(pagination: {{cursor: null, limit: {int(limit) + 1}}}, search: {{query: "{_q(term)}"}}) {{
        items {{ id number description costPrice listPrice }}
      }}
    }}'''
    items = (_gql(q).get("products") or {}).get("items") or []
    flere = max(0, len(items) - int(limit))
    return items[:int(limit)], flere


def product_by_ean(ean: str):
    """Find præcis vare ud fra stregkode/EAN-nummer (varens 'number'-felt)."""
    items, _ = search_products(ean, limit=10)
    for p in items:
        if str(p.get("number") or "").strip() == str(ean).strip():
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
    if identifier is not None:
        felter.append(f'identifier: "{_q(str(identifier))}"')
    if product_number:
        felter.append(f'productNumber: "{_q(str(product_number))}"')
    if description:
        felter.append(f'description: "{_q(description)}"')
    q = f'mutation {{ createCaseMaterial(input: {{{", ".join(felter)}}}) {{ id }} }}'
    return _gql(q).get("createCaseMaterial") or {}

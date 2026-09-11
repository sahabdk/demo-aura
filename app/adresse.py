"""Adressevask via Danmarks Adresseregister (DAWA/Dataforsyningen) - gratis, ingen noegle.

Talegenkendelsen hoerer af og til forkert ("han bor i Kongensgade" -> "Hansborg i Kongesked").
Foer en adresse gemmes, slaas den op i DAWA, som retter stavefejl og smider vroevleord vaek:
    "Hansborg i Kongesked 45, 3000 Hillerød" -> "Kongensgade 45, 3000 Hillerød"

Kategorier fra datavask:
    A = praecis match, B = sikkert match (rettet stavning/format), C = usikkert - spoerg brugeren.
"""
import re
import requests

_URL = "https://api.dataforsyningen.dk/datavask/adresser"
_CACHE = {}


def _formater(a):
    """DAWA-adresse -> (vejnavn husnr [etage. doer], postnr, by)."""
    vej = f"{a.get('vejnavn') or ''} {a.get('husnr') or ''}".strip()
    etage, doer = a.get("etage"), a.get("dør")
    if etage or doer:
        vej += ", " + " ".join(x for x in (f"{etage}." if etage else "", doer or "") if x)
    return vej, str(a.get("postnr") or ""), a.get("postnrnavn") or ""


def vask(tekst):
    """Slaa en fri adressetekst op. Returnerer dict eller None (ved netvaerksfejl/tomt):
       {"kategori": "A"/"B"/"C", "adresse": ..., "postnr": ..., "by": ..., "fuld": "..."}"""
    t = re.sub(r"\s+", " ", (tekst or "")).strip(" ,.")
    if len(t) < 4:
        return None
    if t in _CACHE:
        return _CACHE[t]
    try:
        r = requests.get(_URL, params={"betegnelse": t}, timeout=6)
        if not r.ok:
            return None
        d = r.json() or {}
        res = d.get("resultater") or []
        if not res:
            return None
        a = (res[0].get("aktueladresse") or res[0].get("adresse") or {})
        vej, postnr, by = _formater(a)
        if not vej:
            return None
        ud = {"kategori": d.get("kategori") or "C", "adresse": vej, "postnr": postnr, "by": by,
              "fuld": f"{vej}, {postnr} {by}".strip()}
        _CACHE[t] = ud
        return ud
    except Exception:
        return None


def normaliser(adresse, postnr="", by=""):
    """Best-effort: giv (adresse, postnr, by) tilbage - rettet hvis DAWA er sikker (A/B),
    ellers uaendret. Bruges hvor vi ikke vil afbryde flowet med spoergsmaal."""
    v = vask(f"{adresse}, {postnr} {by}")
    if v and v["kategori"] in ("A", "B"):
        return v["adresse"], v["postnr"] or postnr, v["by"] or by, v
    return adresse, postnr, by, v

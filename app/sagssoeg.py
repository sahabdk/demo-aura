"""Find ÅBNE sager ud fra det et menneske husker: vejnavn (uden husnummer), kundens navn, by eller
opgaven - fx "sagen på Solvej", "hos Hansen", "tavlen i Kolding", "Damgrenen".

Der søges på tværs af alle åbne sager i:
  - sagens beskrivelse (arbejdsstedets adresse står på første linje + selve opgaven)
  - sagens leveringsadresse
  - kundens navn, adresse og by
Hvert søgeord matches ordret, som begyndelsen af et ord ("solvej" ~ "solvejen") eller med tolerance for
stave-/hørefejl ("damgreen" ~ "damgrenen"). Husnumre og postnumre skal passe præcist.
Resultat: (sagsnummer ved ENTYDIGT match ellers None, [forslag]) - forslagene er de bedste åbne sager.
"""
import difflib
import re

from . import ordrestyring as os_api

STOPORD = {
    "tilføj", "tilfoej", "gem", "gemme", "gemt", "billede", "billedet", "billeder", "billed", "foto", "fotoet",
    "video", "videoen", "til", "på", "paa", "ordren", "ordre", "ordrer", "sagen", "sag", "sager", "sagerne",
    "dokumentation", "dok", "det", "dette", "her", "hos", "ved", "denne", "den", "upload", "vedhæft",
    "vedhaeft", "og", "med", "i", "a", "af", "fra", "for", "om", "en", "et", "er", "var", "som", "der",
    "kunden", "kunde", "kundens", "hedder", "ham", "hende", "han", "hun", "vi", "jeg", "du", "lige", "lidt",
    "tak", "please", "også", "ogsaa", "ude", "inde", "oppe", "nede", "adressen", "adresse", "vejen",
    "hvor", "hvad", "note", "noten", "bemærkning", "notat", "læg", "laeg", "skriv", "nr", "nummer", "mig",
    "min", "mit", "mine", "vores", "deres", "hans", "hendes", "the",
    # tid/spørgeord - et svar som 'hvad har jeg i morgen?' må ikke ligne en sagssøgning
    "hvem", "hvornår", "hvordan", "hvorfor", "har", "skal", "kan", "vil", "må", "bliver", "blev", "morgen",
    "dag", "idag", "aften", "eftermiddag", "formiddag", "uge", "ugen", "måned", "klokken", "kl", "time",
    "timer", "mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag", "næste", "sidste",
    "nu", "senere", "igen", "ja", "nej", "okay", "ok", "fint", "godt", "super",
}
_ORD = re.compile(r"[0-9a-zæøåéüäö]+")


def _ord(s):
    return _ORD.findall(str(s or "").lower())


def soegeord(tekst):
    return [o for o in _ord(tekst) if o not in STOPORD and (len(o) >= 3 or o.isdigit())]


_ENDELSER = ("erne", "ene", "en", "et", "er", "ne", "e")


def _stamme(t):
    """Fjern dansk bestemt form/flertal: 'tavlen' -> 'tavl', 'lamperne' -> 'lamp' (kun lange ord)."""
    for e in _ENDELSER:
        if t.endswith(e) and len(t) - len(e) >= 4:
            return t[: -len(e)]
    return t


def _tokenscore(t, ord_saet, hay):
    """0..1: hvor godt ét søgeord passer på sagen (også i bøjet form: 'tavlen' ~ 'eltavle')."""
    st = _stamme(t)
    s = _tokenscore1(t, ord_saet, hay)
    if st != t and s < 1.0:
        s = max(s, 0.95 * _tokenscore1(st, ord_saet, hay, min_del=4))
    return s


def _tokenscore1(t, ord_saet, hay, min_del=5):
    if t.isdigit():
        return 1.0 if t in ord_saet else 0.0
    if t in ord_saet:
        return 1.0
    bedst = 0.0
    for w in ord_saet:
        if w.isdigit():
            continue
        if len(t) >= 4 and len(w) >= 4 and (w.startswith(t) or t.startswith(w)):
            bedst = max(bedst, 0.93)          # solvej ~ solvejen, hans ~ hansen
        elif len(t) >= min_del and t in w:
            bedst = max(bedst, 0.9)           # "grenen" i "damgrenen", "tavl" i "eltavle"
        if abs(len(w) - len(t)) <= 3:
            bedst = max(bedst, difflib.SequenceMatcher(None, t, w).ratio())
    if bedst < 0.8 and len(t) >= min_del and t in hay:
        bedst = 0.85
    return bedst


def _aabne_sager_med_tekst():
    kunder = {}
    try:
        kunder = {str(d.get("customer_number")): d for d in os_api.all_debtors()}
    except Exception:
        pass
    lev = {}
    try:
        lev = {str(d.get("id")): d for d in os_api.alle_leveringsadresser()}
    except Exception:
        pass
    ud = []
    for c in os_api.cases_paged():
        if os_api.is_closed(c):
            continue
        d = kunder.get(str(c.get("customer_number"))) or {}
        la = lev.get(str(c.get("delivery_address") or "")) or {}
        lev_txt = " ".join(str(la.get(k) or "") for k in ("address", "postalcode", "city")).strip()
        besk = (c.get("description") or "").strip()
        foerste = besk.split("\n")[0].strip().rstrip(",")
        work = f"{besk} {lev_txt}".lower()
        navn = str(d.get("customer_name") or "").lower()
        kadr = f"{d.get('customer_address') or ''} {d.get('customer_city') or ''}".lower()
        hay = f"{work} {navn} {kadr}"
        # arbejdsstedet: leveringsadresse > første linje i beskrivelsen (hvis den ligner en adresse) > kundens
        adr = lev_txt or (foerste if re.search(r"\d", foerste) and len(foerste) < 60 else "") or \
            " ".join(str(d.get(k) or "") for k in ("customer_address", "customer_city")).strip()
        opgave = "\n".join(besk.split("\n")[1:]).strip() if adr and foerste and foerste in adr else besk
        egen_adresse = bool(lev_txt or (re.search(r"\d", foerste) and len(foerste) < 60))
        ud.append({"sag": c, "hay": hay, "ord": set(_ord(hay)), "kunde": d.get("customer_name") or "",
                   "dele": [(set(_ord(work)), work, 1.0), (set(_ord(navn)), navn, 1.0),
                            # kundens HJEMMEadresse tæller mindre, når sagen har sit eget arbejdssted
                            (set(_ord(kadr)), kadr, 0.8 if egen_adresse else 1.0)],
                   "adresse": adr, "opgave": (opgave or besk)[:60]})
    return ud


def find(tekst, max_forslag=5, kun_blandt=None):
    """-> (sagsnummer|None, forslag). kun_blandt = [sagsnumre] begrænser søgningen (svar på et spørgsmål)."""
    ord_ = soegeord(tekst)
    if not ord_:
        return None, []
    kandidater = []
    for x in _aabne_sager_med_tekst():
        nr = x["sag"].get("case_number")
        if kun_blandt is not None and str(nr) not in {str(k) for k in kun_blandt}:
            continue
        scores = [max(_tokenscore(t, o, h) * v for o, h, v in x["dele"]) for t in ord_]
        ramt = [s for s in scores if s >= 0.8]
        if not ramt or max(ramt) < 0.85:
            continue
        kandidater.append((len(ramt), sum(ramt), int(x["sag"].get("created_at") or 0), x))
    if not kandidater:
        return None, []
    kandidater.sort(key=lambda k: (k[0], round(k[1], 2), k[2]), reverse=True)
    forslag = [{"sagsnummer": k[3]["sag"].get("case_number"), "kunde": k[3]["kunde"],
                "adresse": k[3]["adresse"], "beskrivelse": k[3]["opgave"],
                "score": round(k[1] / len(ord_), 2)} for k in kandidater[:max_forslag]]
    top = kandidater[0]
    naeste = kandidater[1] if len(kandidater) > 1 else None
    entydig = naeste is None or top[0] > naeste[0] or (top[1] - naeste[1] >= 0.15)
    if entydig and top[1] / len(ord_) >= 0.6:
        return forslag[0]["sagsnummer"], forslag
    return None, forslag


def linje(f, i=None):
    """'1) Sag 170 · Nila Selvtest · Solvej 7, 8000 Aarhus C — To stikkontakter'"""
    dele = [f"Sag {f['sagsnummer']}"] + [x for x in (f.get("kunde"), f.get("adresse")) if x]
    t = " · ".join(dele)
    if f.get("beskrivelse"):
        t += f" — {f['beskrivelse']}"
    return (f"{i}) " if i else "") + t


def vaelg_fra_svar(svar, forslag):
    """Brugerens svar på 'hvilken sag?': sagsnummer, placering ('2', 'den anden') eller navn/vej.
    -> sagsnummer eller None."""
    t = (svar or "").strip().lower()
    numre = [str(f["sagsnummer"]) for f in forslag]
    tal = re.findall(r"\d+", t)
    for n in tal:
        if n in numre:
            return n
    placering = {"første": 1, "foerste": 1, "anden": 2, "andet": 2, "tredje": 3, "fjerde": 4, "femte": 5,
                 "sidste": len(forslag)}
    for ordet, pos in placering.items():
        if re.search(rf"\b{ordet}\b", t) and 1 <= pos <= len(forslag):
            return numre[pos - 1]
    if len(tal) == 1 and 1 <= int(tal[0]) <= len(forslag) and len(tal[0]) <= 2:
        return numre[int(tal[0]) - 1]
    if tal and len(tal[0]) >= 2 and not soegeord(re.sub(r"\d+", "", t)):
        return tal[0]   # et rigtigt sagsnummer, der ikke var blandt forslagene
    if forslag:
        nr, _ = find(t, kun_blandt=numre)
        if nr:
            return str(nr)
    return None

"""Forsinkelses-/ankomst-SMS ud fra en kort besked (portet fra Nila 3.0 / Gjern EL):

    "jeg bliver 5 min forsinket hos Thomas på Primavej 15"
    "kommer 20 min senere til Hansen"
    "er der kl. 14.30 hos Kongensgade 72"

Nila finder aftalen (planlagt tid i ordrestyring i dag/i morgen) eller den åbne sag på
adressen/kunden, slår kundens mobilnummer op og sender en pæn SMS (GatewayAPI via retell.send_sms).
Er der flere mulige kunder, spørger hun først (via agenten). SMS'en noteres som bemærkning på sagen.
Teksterne kan rettes i Pilly → Skabeloner (skabelon_sms_forsinket / _forsinket_tid / _ankomst).
"""
import difflib
import os
import re
from datetime import timedelta

from . import db, retell
from . import ordrestyring as os_api
from .config import TZ, now_local

FIRMA_NAVN = os.environ.get("FIRMA_NAVN", "")

STD_FORSINKET = ("Hej{navn}. Vi bliver desværre ca. {minutter} minutter forsinket til vores aftale i dag. "
                 "Vi beklager ventetiden og ses snart. Mvh {firma}")
STD_FORSINKET_TID = ("Hej{navn}. Vi bliver desværre lidt forsinket og forventer at være hos dig ca. kl. {tid}. "
                     "Vi beklager ventetiden. Mvh {firma}")
STD_ANKOMST = "Hej{navn}. Vi er hos dig ca. kl. {tid} i dag. Ses snart! Mvh {firma}"

_STOP = {"jeg", "vi", "bliver", "blive", "er", "kommer", "kom", "til", "hos", "på", "paa", "ved", "i", "og",
         "ca", "cirka", "min", "minutter", "minut", "forsinket", "forsinkelse", "senere", "sent", "sen",
         "kl", "klokken", "der", "derovre", "derhen", "nu", "lige", "ham", "hende", "dem", "sig", "at",
         "skriv", "send", "sms", "en", "et", "besked", "kunden", "aftalen", "aftale", "om", "time", "timer",
         "morgen", "idag", "dag", "eftermiddag", "formiddag", "over", "middag", "aften"}


def norm_tlf(nr):
    d = retell._norm_tlf(nr)
    return d if len(d) >= 8 else ""


def pn(nr):
    d = retell._norm_tlf(nr)
    return f"{d[:2]} {d[2:4]} {d[4:6]} {d[6:8]}" if len(d) == 8 else (str(nr) or "")


def parse(tekst):
    """-> {'type': 'forsinket'|'ankomst', 'minutter': int|None, 'tid': 'HH:MM'|None, 'sted': str}"""
    t = (tekst or "").strip()
    t = re.sub(r"(?:en\s+)?(?:½|halv)\s*time\b", "30 min", t, flags=re.I)
    tl = t.lower()
    ud = {"type": "forsinket", "minutter": None, "tid": None, "sted": ""}
    if not any(o in tl for o in ("forsink", "senere", "for sent", "sent på", "bliver sen")):
        if any(o in tl for o in ("er der", "kommer", "ankom", "er hos", "er fremme", "på vej")):
            ud["type"] = "ankomst"
    rest = t
    m = re.search(r"(\d+)\s*(?:min|minut)\w*", tl)
    if m:
        ud["minutter"] = int(m.group(1))
        rest = rest[:m.start()] + " " + rest[m.end():]
    else:
        m = re.search(r"\b(halvanden|en halv|et halvt|en|et|\d+)\s*(?:halv\s*)?time\w*", tl)
        if m:
            g = m.group(1)
            if g == "halvanden":
                ud["minutter"] = 90
            elif g in ("en halv", "et halvt"):
                ud["minutter"] = 30
            else:
                tal = 1 if g in ("en", "et") else int(g)
                ud["minutter"] = tal * 60 + (30 if "halv" in m.group(0)[len(g):] else 0)
            rest = rest[:m.start()] + " " + rest[m.end():]
    m = re.search(r"(?:kl\.?|klokken)\s*(\d{1,2})(?:[.:,]\s*(\d{2}))?", tl)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if 0 <= h < 24 and 0 <= mi < 60:
            ud["tid"] = f"{h:02d}:{mi:02d}"
        rest = rest[:m.start()] + " " + rest[m.end():]
    ms = re.search(r"\b(?:hos|til|ved)\s+(.+)$", rest, re.I)
    sted = ms.group(1) if ms else rest
    ord_ = [o for o in re.split(r"[\s,]+", sted) if o]
    ord_ = [o for o in ord_ if o.lower().strip(".!?") not in _STOP]
    ud["sted"] = " ".join(ord_).strip(" .,!?")
    return ud


def _kundeliste():
    return {str(d.get("customer_number")): d for d in os_api.all_debtors()}


def _score(sted, kunde):
    s = (sted or "").lower()
    if not s or not kunde:
        return 0.0
    navn = (kunde.get("customer_name") or "").lower()
    adr = f"{kunde.get('customer_address') or ''} {kunde.get('customer_city') or ''}".lower()
    hay = f"{navn} {adr}"
    tokens = [x for x in s.split() if len(x) >= 2]
    if not tokens:
        return 0.0
    hits = 0.0
    for x in tokens:
        if x in hay:
            hits += 1
        else:
            best = max([difflib.SequenceMatcher(None, x, y).ratio() for y in hay.split()] + [0])
            if best >= 0.8:
                hits += 0.8
    return hits / len(tokens)


def find_aftale(sted, kun_i_dag=True):
    """Dagens/morgendagens planlagte sager -> åbne sager -> kunder."""
    from . import os_graphql as os_gql
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    kunder = _kundeliste()
    nu = now_local()
    tz = ZoneInfo(TZ)
    kandidater = []
    try:
        start = nu.replace(hour=0, minute=0, second=0, microsecond=0)
        slut = start + timedelta(days=1 if (kun_i_dag and nu.hour < 17) else 2)
        s = int(start.replace(tzinfo=tz).timestamp())
        e = int(slut.replace(tzinfo=tz).timestamp())
        sager = {str(cc.get("case_number")): cc for cc in os_api.cases_paged()}
        for x in os_gql.planned_events_between(s, e):
            c = sager.get(str(x["sagsnummer"])) or {}
            kn = str(c.get("customer_number") or "")
            k = kunder.get(kn)
            if not k:
                continue
            sc = max(_score(sted, k), _score(sted, {"customer_name": c.get("description") or ""}) * 0.9)
            if sc >= 0.5:
                kandidater.append({"customer_number": kn, "navn": k.get("customer_name"),
                                   "adresse": k.get("customer_address"), "sagsnummer": x["sagsnummer"],
                                   "planlagt": _dt.fromtimestamp(x["startTime"], tz).strftime("%d/%m kl. %H:%M"),
                                   "score": sc + 0.2})
    except Exception as ex:
        print(f"[forsinkelse] plan-opslag fejlede: {str(ex)[:120]}", flush=True)
    kilde = "planlagt"
    if not kandidater:
        kilde = "aabne sager"
        for c in os_api.cases_paged():
            if os_api.is_closed(c):
                continue
            kn = str(c.get("customer_number") or "")
            k = kunder.get(kn)
            if not k:
                continue
            sc = max(_score(sted, k), _score(sted, {"customer_name": c.get("description") or ""}) * 0.9)
            if sc >= 0.6:
                kandidater.append({"customer_number": kn, "navn": k.get("customer_name"),
                                   "adresse": k.get("customer_address"), "sagsnummer": c.get("case_number"),
                                   "planlagt": "", "score": sc})
    if not kandidater:
        kilde = "kunder"
        for kn, k in kunder.items():
            sc = _score(sted, k)
            if sc >= 0.7:
                kandidater.append({"customer_number": kn, "navn": k.get("customer_name"),
                                   "adresse": k.get("customer_address"), "sagsnummer": "",
                                   "planlagt": "", "score": sc})
    bedste = {}
    for k in kandidater:
        if k["customer_number"] not in bedste or k["score"] > bedste[k["customer_number"]]["score"]:
            bedste[k["customer_number"]] = k
    kandidater = sorted(bedste.values(), key=lambda k: -k["score"])[:4]
    for k in kandidater:
        k["telefon"] = _telefon(k["customer_number"])
    return kandidater, kilde


def _telefon(kn):
    d = _kundeliste().get(str(kn)) or {}
    for f in ("customer_mobile", "customer_telephone"):
        if norm_tlf(d.get(f)):
            return norm_tlf(d.get(f))
    try:
        for c in os_api.debtor_contacts(kn):
            for f in ("mobile", "telephone"):
                if norm_tlf(c.get(f)):
                    return norm_tlf(c.get(f))
    except Exception:
        pass
    return ""


def byg_tekst(navn, typ, minutter=None, tid=None):
    fornavn = (navn or "").split()[0] if navn and not (navn or "").isupper() else ""
    hilsen = f" {fornavn}" if fornavn and len(fornavn) > 1 else ""
    if typ == "ankomst" and tid:
        skab = db.get_meta("skabelon_sms_ankomst") or STD_ANKOMST
    elif tid:
        skab = db.get_meta("skabelon_sms_forsinket_tid") or STD_FORSINKET_TID
    else:
        skab = db.get_meta("skabelon_sms_forsinket") or STD_FORSINKET
    if not FIRMA_NAVN:
        skab = skab.replace(" Mvh {firma}", "")
    return (skab.replace("{navn}", hilsen).replace("{minutter}", str(minutter or 15))
            .replace("{tid}", tid or "").replace("{firma}", FIRMA_NAVN)).strip()


def meld(tekst, ctx, customer_number=None, minutter=None, tid=None, typ=None):
    """Hele flowet. Returnerer dict til agenten (resultat / kandidater / fejl)."""
    p = parse(tekst)
    minutter = minutter or p["minutter"]
    tid = tid or p["tid"]
    typ = typ or p["type"]
    if customer_number:
        k = _kundeliste().get(str(customer_number)) or {}
        kand = [{"customer_number": str(customer_number), "navn": k.get("customer_name"),
                 "adresse": k.get("customer_address"), "sagsnummer": "", "planlagt": "",
                 "telefon": _telefon(customer_number), "score": 1.0}]
    else:
        if not p["sted"]:
            return {"resultat": "Hvem/hvor gælder det? Sig kundens navn eller adressen (fx 'hos Thomas på Primavej 15')."}
        kand, _kilde = find_aftale(p["sted"])
        if not kand:
            return {"resultat": f"Jeg fandt ingen aftale eller kunde der passer på '{p['sted']}'. "
                                "Sig navnet eller adressen præcist, eller giv mig telefonnummeret."}
        entydig = len(kand) == 1 or kand[0]["score"] >= kand[1]["score"] + 0.25
        if not entydig:
            return {"resultat": "Flere kunder passer - spørg KORT hvilken (vis navn + vej). Kald derefter "
                                "meld_forsinkelse igen med customer_number for den valgte.",
                    "kandidater": [{"customer_number": k["customer_number"], "navn": k["navn"],
                                    "adresse": k["adresse"], "planlagt": k["planlagt"]} for k in kand]}
        kand = kand[:1]
    k = kand[0]
    if not k.get("telefon"):
        return {"resultat": f"{k.get('navn')} har intet mobilnummer på kundekortet, så jeg kan ikke sende "
                            "SMS. Sig nummeret, så sender jeg (sms_til_kunde med telefon)."}
    if typ == "ankomst" and not tid:
        return {"resultat": "Hvad tid er du der? (fx 'kl. 14.30')"}
    tekst_sms = byg_tekst(k.get("navn"), typ, minutter, tid)
    import hashlib
    import time as _t
    fp = hashlib.sha256(f"{k['telefon']}:{tekst_sms}".encode()).hexdigest()[:16]
    if db.get_meta("sms_kunde_" + fp) and _t.time() - float(db.get_meta("sms_kunde_" + fp)) < 1200:
        return {"resultat": "Den SMS er allerede sendt til kunden for lidt siden - sender ikke igen.", "dublet": True}
    try:
        ret = retell.send_sms(k["telefon"], tekst_sms, kontekst=f"forsinkelses-sms til {k.get('navn')}")
    except Exception as e:
        return {"fejl": f"SMS'en blev IKKE sendt: {str(e)[:150]}"}
    db.log_handling(ctx.get("telegram_id"), ctx.get("navn"), ctx.get("rolle"), f"forsinkelses-sms {ret}",
                    f"{k.get('navn')} {pn(k['telefon'])}: {tekst_sms}")
    if ret != "sent":
        return {"resultat": f"SMS'en blev IKKE sendt ({ret} - testtilstand eller sms slået fra). Sig det ærligt. "
                            f"Teksten ville være: \"{tekst_sms}\""}
    db.set_meta("sms_kunde_" + fp, str(_t.time()))
    if k.get("sagsnummer"):
        try:
            os_api.add_remark(k["sagsnummer"], f"SMS til kunde: {tekst_sms}", now_local().strftime("%d-%m-%Y"))
        except Exception:
            pass
    hvor = f" (aftale {k['planlagt']})" if k.get("planlagt") else ""
    return {"resultat": f"SMS sendt til {k.get('navn')} ({pn(k['telefon'])}){hvor}: \"{tekst_sms}\". "
                        "Bekræft kort med én sætning."}

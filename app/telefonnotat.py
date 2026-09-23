"""Telefonnotat: Aura lytter med paa firmaets opkald og tager noter.

Flow (Twilio-nummer, firmaets nummer viderestilles hertil):
  1) Kunden ringer -> kort besked ("samtalen skrives ned") -> ringer ud til medarbejdernes
     mobiler (alle paa en gang). Samtalen optages fra der tages.
  2) Naar der laegges paa -> lyden hentes, skrives ud (OpenAI), SLETTES hos Twilio,
     nummeret matches mod ALLE telefonnumre i systemet (kundekort + kontakter),
     GPT laver et kort resume -> Telegram til den der tog den (+ lederen).
     Beskeden slutter ALTID med et spoergsmaal - Aura goer intet foer der er bekraeftet.
  3) Tager ingen den -> almindelig telefonsvarer. Beskeden skrives ud og sendes paa samme maade.

Indstillinger (Pilly-dashboardet, ✉️ Skabeloner):
  telefon_mobiler   "Navn:+4520123456:telegram_id, Navn2:+45..."  (hvem der ringes ud til)
  telefon_intro     tekst der siges FOER der stilles om (GDPR-oplysning)
  telefon_svarer    telefonsvarer-tekst
  telefon_private   numre der ALDRIG optages (komma-sep.)
Miljoe: TWILIO_SID, TWILIO_TOKEN (findes allerede til saldo-tjek).
"""
import io
import json
import os
import re
import threading
import time
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape

import requests

from .config import (APP_BASE_URL, LEADER_GROUP_CHAT_ID, OPENAI_MODEL, OPENAI_STT_MODEL,
                     WEBHOOK_SECRET)
from . import db, telegram, retell

TWILIO_SID = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
STEMME = os.environ.get("TWILIO_STEMME", "Polly.Naja")   # dansk kvindestemme hos Twilio
RING_SEK = int(os.environ.get("TELEFON_RING_SEK", "25"))

STD_INTRO = ("Hej, du har ringet til {firma}. Samtalen bliver skrevet ned, så vi husker aftalen. "
             "Vent venligst, mens vi stiller om.")
STD_SVARER = ("Vi kan desværre ikke tage telefonen lige nu. Læg en besked med dit navn og "
              "hvad det drejer sig om efter tonen, så vender vi tilbage hurtigst muligt.")
STD_SVARER_OPTAGET = ("Vi taler i telefon lige nu. Læg en besked med dit navn og hvad det drejer "
                      "sig om efter tonen, så ringer vi tilbage, så snart vi er færdige.")
STD_INTRO_UD = "Hej, det er {firma}. Samtalen skrives ned, så vi husker aftalen."


# ---------- indstillinger ----------

def _firma():
    return os.environ.get("FIRMA_NAVN", "Aura")


def tekst(noegle, standard):
    """Redigerbar tekst: aldrig sat -> standard; sat til TOM -> tom (= slaaet fra)."""
    v = db.get_meta(noegle)
    return (standard if v is None else v).replace("{firma}", _firma()).strip()


def mobiler():
    """[{navn, nummer(+45...), telegram_id}] fra meta 'telefon_mobiler'."""
    ud = []
    for del_ in (db.get_meta("telefon_mobiler") or "").split(","):
        bidder = [b.strip() for b in del_.split(":")]
        if len(bidder) < 2 or not bidder[1]:
            continue
        nr = retell._norm_tlf(bidder[1])
        if len(nr) == 8:
            nr = "45" + nr
        ud.append({"navn": bidder[0], "nummer": "+" + nr,
                   "telegram_id": bidder[2] if len(bidder) > 2 else ""})
    return ud


def _privat(nummer):
    n8 = retell._norm_tlf(nummer)[-8:]
    return any(retell._norm_tlf(p)[-8:] == n8 for p in (db.get_meta("telefon_private") or "").split(",") if p.strip())


def _base():
    return (APP_BASE_URL or "").rstrip("/")


def _url(sti):
    return f"{_base()}/twilio/{WEBHOOK_SECRET}/{sti}"


def _say(tekst):
    return f'<Say language="da-DK" voice="{STEMME}">{escape(tekst)}</Say>'


def _h(s):
    """HTML-escape til Telegram (parse_mode=HTML): &, <, > i kundens tekst maa ikke tolkes som tags."""
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- TwiML (det Twilio skal goere) ----------

_AKTIVE_RINGUD = {}   # {8 cifre (kundens nummer): tidspunkt} - opkald vi lige nu ringer videre


def _n8(nr):
    return retell._norm_tlf(nr)[-8:]


def _loekke(form, mob):
    """Er dette vores EGET ring-ud, der er blevet viderestillet tilbage til Twilio?
    (Sker hvis hovednummeret baade er viderestillet OG staar som modtager.)"""
    fra = _n8(form.get("From") or "")
    nu = time.time()
    for k, t in list(_AKTIVE_RINGUD.items()):
        if nu - t > 90:
            _AKTIVE_RINGUD.pop(k, None)
    if fra and fra in _AKTIVE_RINGUD:
        return True
    # netvaerk der saetter afsender = det viderestillede nummer
    if _AKTIVE_RINGUD and any(_n8(m["nummer"]) == fra for m in mob):
        return True
    return False


def twiml_indgaaende(form):
    """Foerste svar naar telefonen ringer: intro + ring ud til mobilerne (med optagelse)."""
    fra = form.get("From") or ""
    if not db.funktion_til("telefonnotat"):
        return f"<Response>{_say(STD_SVARER)}<Record maxLength=\"120\" playBeep=\"true\"/></Response>"
    mob = mobiler()
    if _loekke(form, mob):
        # afvis DENNE gren som optaget -> det oprindelige Dial faar 'busy' -> telefonsvarer
        db.log_handling("", "Aura (telefon)", "system", "viderestillings-loekke",
                        f"{_pn(fra)}: ring-ud kom retur til Twilio - hovednummer er baade "
                        "viderestillet og modtager")
        if not db.get_meta("telefon_loekke_advaret"):
            db.set_meta("telefon_loekke_advaret", "1")
            try:
                telegram.send_leader(LEADER_GROUP_CHAT_ID,
                    "⚠️ Telefon: ring-ud til hovednummeret løber i ring (nummeret er både viderestillet "
                    "til Nila og sat som modtager). Kunden fik telefonsvareren. Brug et andet "
                    "modtagernummer (fx telefonens 2. nummer) eller skift til 'kun ubesvarede'.")
            except Exception:
                pass
        return '<Response><Reject reason="busy"/></Response>'
    # 'kun ubesvarede'-tilstand (*61*): hovednummeret er allerede ringet - gaa direkte til svarer
    if db.get_meta("telefon_tilstand") == "svarer":
        return twiml_telefonsvarer()
    intro = tekst("telefon_intro", STD_INTRO)   # tom = ingen besked, stil om med det samme
    if not mob:
        return twiml_telefonsvarer()
    # En af vores egne mobiler ringer til Nilas nummer (fx ring-tilbage fra opkaldslisten)
    if fra and any(_n8(m["nummer"]) == _n8(fra) for m in mob):
        # En kollega ringer firmanummeret (viderestillet til Nila): stil om til de ANDRE mobiler -
        # uden intro og UDEN optagelse (internt opkald = aldrig notat).
        andre = [m for m in mob if _n8(m["nummer"]) != _n8(fra)]
        if andre:
            til_nr = form.get("To") or ""
            caller = f' callerId="{escape(fra)}"' if fra.startswith("+") else (
                f' callerId="{escape(til_nr)}"' if til_nr.startswith("+") else "")
            return (f'<Response><Dial timeout="{RING_SEK}"{caller}>'
                    + "".join(f"<Number>{m['nummer']}</Number>" for m in andre) + "</Dial></Response>")
        return (f"<Response>{_say('Hej. Det her er Nilas nummer. Ring kunden op fra notatet i Telegram.')}"
                "<Hangup/></Response>")
    if fra:
        _AKTIVE_RINGUD[_n8(fra)] = time.time()
    optag = "" if _privat(fra) else (
        f' record="record-from-answer-dual" recordingStatusCallback="{_url("optagelse?type=samtale")}"'
        f' recordingStatusCallbackEvent="completed"')
    # Afsender paa ring-ud: 'erhverv' = Nilas eget nummer (gem det som kontakt "E - Erhverv" med egen
    # ringetone, saa erhverv og privat aldrig forveksles) - 'kunde' = kundens rigtige nummer.
    til = form.get("To") or ""
    if til.startswith("+") and db.get_meta("telefon_twilio_nummer") != til:
        db.set_meta("telefon_twilio_nummer", til)   # huskes til ring-op
    if (db.get_meta("telefon_visning") or "kunde") == "erhverv" or not fra.startswith("+"):
        caller = f' callerId="{escape(til)}"' if til.startswith("+") else ""
    else:
        caller = f' callerId="{escape(fra)}"'   # standard: kundens rigtige nummer paa skaermen
    # Hvisk til den der tager den ("Erhverv. Thomas Hansen.") - kunden hoerer det ikke
    hvisk = ""
    if db.get_meta("telefon_hvisk") == "1":   # standard FRA: at tage roeret skal foeles som altid
        hvisk = f' url="{escape(_url("hvisk") + "?fra=" + quote(fra))}" method="POST"'
    numre = "".join(f"<Number{hvisk}>{m['nummer']}</Number>" for m in mob)
    if db.get_meta("telefon_ringbesked") != "0":
        # Foerste linje sendes NU (synkront, ~0.3 s) - saa banneret er paa skaermen, foer
        # Twilio begynder at ringe mobilen op. Kundekortet fyldes paa bagefter i en traad.
        try:
            sendte = _ringer_nu_hurtig(fra, mob)
            _banner_husk(fra, sendte)
            threading.Thread(target=_ringer_nu_detaljer, args=(fra, sendte), daemon=True).start()
        except Exception as e:
            print(f"[ringer_nu] fejlede: {str(e)[:120]}", flush=True)
    # Lille pause foer ring-ud, saa Telegram-banneret altid er paa skaermen foerst (std. 2 sek.)
    try:
        pause = max(0, min(8, int(db.get_meta("telefon_forsinkelse") or 2)))
    except (ValueError, TypeError):
        pause = 2
    return (f"<Response>{_say(intro) if intro else ''}"
            f"{f'<Pause length={chr(34)}{pause}{chr(34)}/>' if pause else ''}"
            f'<Dial timeout="{RING_SEK}"{caller}{optag} action="{_url("efter-dial")}" method="POST">'
            f"{numre}</Dial></Response>")


def _opkalder_info(fra):
    """Lynopslag i telefon-indekset (ingen API-kald): {kn, kunde, kontakt, adresse} eller None."""
    try:
        idx = json.loads(db.get_meta("telefon_indeks") or "{}")
        return idx.get(_n8(fra)) or None
    except Exception:
        return None


def _hvem(fra):
    if not fra or not _n8(fra):
        return "skjult nummer", None
    info = _opkalder_info(fra)
    if not info:
        return "ukendt nummer", None
    navn = info.get("kunde") or ""
    if info.get("kontakt"):
        return f"{info['kontakt']} fra {navn} (kundenr. {info.get('kn')})", info
    return f"{navn} (kundenr. {info.get('kn')})", info


_KORT_CACHE = {"ts": 0.0, "kort": {}}


def byg_kundekort_indeks():
    """Scheduler (hvert 10. min): kundekort for ALLE kunder med sager/fakturaer -> hukommelsen,
    saa 'ringer nu'-beskeden kan indeholde alt fra foerste sekund uden API-kald."""
    from . import ordrestyring as os_api
    from . import os_graphql as os_gql
    from datetime import datetime as _dt
    kort = {}
    try:
        adresser = {}
        for d in os_api.all_debtors():
            adr = " ".join(x for x in (d.get("customer_address"), str(d.get("customer_postalcode") or ""),
                                       d.get("customer_city")) if x).strip()
            if adr:
                adresser[str(d.get("customer_number"))] = adr
        statusser = {str(s.get("id")): (s.get("text") or "") for s in os_api.case_statuses()}
        lukket = str(os_api.closed_status_id())
        plan = {}
        for p in (os_gql._PLAN_CACHE.get("rows") or []):
            plan.setdefault(str(p.get("sagsnummer")), p)
        pr_kunde = {}
        for c in os_api.cases_paged():
            pr_kunde.setdefault(str(c.get("customer_number")), []).append(c)
        forfaldne = {}
        try:
            for f in os_api.overdue_unpaid_invoices():
                k = str(f.get("customer_number") or f.get("debtor_number") or "")
                forfaldne[k] = forfaldne.get(k, 0) + 1
        except Exception:
            pass
        for kn in set(list(pr_kunde) + list(forfaldne)):
            linjer = []
            if adresser.get(kn):
                linjer.append(f"📍 {adresser[kn]}")
            mine = pr_kunde.get(kn, [])
            for c in [c for c in mine if str(c.get("status")) != lukket][:3]:
                nr = c.get("case_number")
                besk = (c.get("description") or "").strip().replace("\n", " ")[:50]
                p = plan.get(str(nr))
                tid = (" · planlagt " + _dt.fromtimestamp(p["startTime"]).strftime("%d/%m kl. %H:%M")
                       if p and p.get("startTime") else "")
                linjer.append(f"🔧 Sag {nr} ({statusser.get(str(c.get('status')), '')}){tid}: {besk}")
            for c in [c for c in mine if str(c.get("status")) == lukket][:1]:
                besk = (c.get("description") or "").strip().replace("\n", " ")[:50]
                try:
                    dato = _dt.fromtimestamp(int(c.get("created_at") or 0)).strftime("%d/%m-%y")
                except (ValueError, TypeError, OSError):
                    dato = ""
                linjer.append(f"✅ Sidst: sag {c.get('case_number')} {dato}: {besk}")
            if forfaldne.get(kn):
                n = forfaldne[kn]
                linjer.append(f"⚠️ {n} forfalden faktura" + ("er" if n > 1 else ""))
            kort[kn] = linjer
        _KORT_CACHE.update(ts=time.time(), kort=kort)
        print(f"[kundekort_indeks] {len(kort)} kundekort klar", flush=True)
    except Exception as e:
        print(f"[kundekort_indeks] fejlede: {str(e)[:150]}", flush=True)


def _kundekort_kort(kn, kun_cache=False):
    """Lyn-overblik over kunden: fra det forudbyggede indeks (0 ms) - ellers live fra cachen."""
    if _KORT_CACHE["kort"] and time.time() - _KORT_CACHE["ts"] < 1200:
        return _KORT_CACHE["kort"].get(str(kn)) or []
    if kun_cache:
        return None
    from . import ordrestyring as os_api
    from . import os_graphql as os_gql
    from datetime import datetime as _dt
    linjer = []
    try:
        d = next((x for x in os_api.all_debtors() if str(x.get("customer_number")) == str(kn)), None) or {}
        adr = " ".join(x for x in (d.get("customer_address"), str(d.get("customer_postalcode") or ""),
                                   d.get("customer_city")) if x).strip()
        if adr:
            linjer.append(f"📍 {adr}")
    except Exception:
        pass
    try:
        statusser = {str(s.get("id")): (s.get("text") or "") for s in os_api.case_statuses()}
        lukket = str(os_api.closed_status_id())
        plan = {}
        for p in (os_gql._PLAN_CACHE.get("rows") or []):
            plan.setdefault(str(p.get("sagsnummer")), p)
        mine = [c for c in os_api.cases_paged() if str(c.get("customer_number")) == str(kn)]
        aabne = [c for c in mine if str(c.get("status")) != lukket][:3]
        lukkede = [c for c in mine if str(c.get("status")) == lukket][:1]
        for c in aabne:
            nr = c.get("case_number")
            besk = (c.get("description") or "").strip().replace("\n", " ")[:50]
            st = statusser.get(str(c.get("status")), "")
            p = plan.get(str(nr))
            tid = ""
            if p and p.get("startTime"):
                tid = " · planlagt " + _dt.fromtimestamp(p["startTime"]).strftime("%d/%m kl. %H:%M")
            linjer.append(f"🔧 Sag {nr} ({st}){tid}: {besk}")
        for c in lukkede:
            besk = (c.get("description") or "").strip().replace("\n", " ")[:50]
            try:
                dato = _dt.fromtimestamp(int(c.get("created_at") or 0)).strftime("%d/%m-%y")
            except (ValueError, TypeError, OSError):
                dato = ""
            linjer.append(f"✅ Sidst: sag {c.get('case_number')} {dato}: {besk}")
        if not mine:
            linjer.append("🔧 Ingen sager i de seneste ~500")
    except Exception as e:
        print(f"[ringer_nu] sagsopslag fejlede: {str(e)[:100]}", flush=True)
    try:
        forf = [f for f in os_api.overdue_unpaid_invoices()
                if str(f.get("customer_number") or f.get("debtor_number") or "") == str(kn)]
        if forf:
            linjer.append(f"⚠️ {len(forf)} forfalden faktura" + ("er" if len(forf) > 1 else ""))
    except Exception:
        pass
    return linjer


_BANNERE = {}   # 8 cifre -> [(chat_id, message_id)] for 'ringer nu'-bannere, slettes naar notatet lander


def _banner_husk(fra, sendte):
    ids = [(c, m) for c, m, _, _ in (sendte or []) if m]
    if ids:
        _BANNERE[_n8(fra)] = {"ts": time.time(), "ids": ids}
    # ryd gamle (over 2 timer) saa dict'en ikke vokser
    for k, v in list(_BANNERE.items()):
        if time.time() - v["ts"] > 7200:
            _BANNERE.pop(k, None)


def _banner_slet(fra):
    b = _BANNERE.pop(_n8(fra), None)
    for c, m in (b or {}).get("ids", []):
        try:
            telegram.delete_message(c, m)
        except Exception:
            pass


def _ringer_nu_tekst(fra):
    hvem, info = _hvem(fra)
    if info:
        navn = (info.get("kontakt") + " / " if info.get("kontakt") else "") + (info.get("kunde") or "")
        return f"📞 E · {navn} ringer — {_pn(fra)} (kundenr. {info.get('kn')})", info
    return f"📞 E · {hvem.capitalize()} ringer — {_pn(fra)}", None


def _ringer_nu_hurtig(fra, mob):
    """Foerste besked til Telegram med det samme - MED kundekort hvis indekset er klar.
    Returnerer [(chat_id, message_id, tekst, info_eller_None_hvis_faerdig)]."""
    tekst, info = _ringer_nu_tekst(fra)
    faerdig = False
    if info:
        klar = _kundekort_kort(info.get("kn"), kun_cache=True)   # 0 ms hvis indekset er bygget
        if klar is not None:
            if klar:
                tekst += "\n" + "\n".join(klar)
            faerdig = True
    sendte = []
    for m in mob:
        if m.get("telegram_id"):
            try:
                mid = telegram.send_and_get_id(m["telegram_id"], tekst)
                sendte.append((m["telegram_id"], mid, tekst, None if faerdig else info))
            except Exception:
                pass
    return sendte


def _ringer_nu_detaljer(fra, sendte):
    """Kun hvis indekset ikke var klar: kundekort haegtes paa bagefter (redigeres, ingen ny notifikation)."""
    if not sendte or not sendte[0][3]:
        return
    info = sendte[0][3]
    try:
        ekstra = _kundekort_kort(info.get("kn"))
    except Exception:
        ekstra = []
    if not ekstra:
        return
    for chat_id, mid, tekst, _ in sendte:
        if not mid:
            continue
        try:
            telegram.edit_message(chat_id, mid, tekst + "\n" + "\n".join(ekstra))
        except Exception:
            pass


def _ringer_nu(fra, mob):
    """(bagudkompatibel) foerste linje + detaljer i et hug."""
    _ringer_nu_detaljer(fra, _ringer_nu_hurtig(fra, mob))


def twiml_hvisk(fra):
    """Spilles KUN for den medarbejder der tager roeret, foer kunden kobles paa."""
    hvem, info = _hvem(fra)
    if info:
        navn = info.get("kunde") or ""
        tekst = f"Erhverv. {info['kontakt'] + ' fra ' if info.get('kontakt') else ''}{navn}."
    else:
        tekst = f"Erhverv. {hvem.capitalize()}."
    return f"<Response>{_say(tekst)}</Response>"


# ---------- ring op via Nila (udgaaende med hovednummer som afsender) ----------

def _e164(nr):
    d = retell._norm_tlf(nr)
    if len(d) == 8:
        d = "45" + d
    return "+" + d if d else ""


def ring_op(mobil, til, navn="", kn=""):
    """Start et udgaaende opkald: Twilio ringer medarbejderens mobil; naar den tages, ringes
    kunden op (TwiML fra /ringop). Returnerer Twilio-svaret."""
    twilio_nr = os.environ.get("TWILIO_NUMBER") or db.get_meta("telefon_twilio_nummer") or ""
    if not twilio_nr:
        raise RuntimeError("Nilas telefonnummer kendes ikke endnu - ring til Twilio-nummeret en gang "
                           "foerst (eller saet TWILIO_NUMBER i Railway)")
    url = _url("ringop") + "?" + urlencode({"til": til, "navn": navn or "", "kn": kn or "",
                                            "tg": mobil.get("telegram_id") or ""})
    r = _tw("POST", "Calls.json", data={"To": mobil["nummer"], "From": twilio_nr,
                                        "Url": url, "Method": "POST", "Timeout": "25"})
    return r.json()


def twiml_ringop(q):
    """Medarbejderen tog Nilas opkald -> ring kunden op med hovednummeret som afsender."""
    til = q.get("til") or ""
    navn = q.get("navn") or ""
    hoved = _e164(db.get_meta("telefon_hovednummer") or "")
    twilio_nr = os.environ.get("TWILIO_NUMBER") or db.get_meta("telefon_twilio_nummer") or ""
    caller = hoved or twilio_nr
    cb = _url("optagelse") + "?" + urlencode({"type": "udgaaende", "kunde": til,
                                              "tg": q.get("tg") or ""})
    intro_kunde = ""
    if tekst("telefon_intro_udgaaende", STD_INTRO_UD):
        intro_kunde = f' url="{escape(_url("intro-kunde"))}" method="POST"'
    optag = "" if _privat(til) else (f' record="record-from-answer-dual" recordingStatusCallback="{escape(cb)}"'
                                     f' recordingStatusCallbackEvent="completed"')
    return (f"<Response>{_say('Erhverv. Ringer op til ' + (navn or _pn(til)) + '.')}"
            f'<Dial callerId="{escape(caller)}" timeout="30"{optag}>'
            f"<Number{intro_kunde}>{escape(til)}</Number></Dial>"
            f"{_say('Kunden tog den ikke.')}</Response>")


def twiml_intro_kunde():
    """Spilles for KUNDEN naar denne tager Nilas udgaaende opkald (GDPR-oplysning)."""
    t = tekst("telefon_intro_udgaaende", STD_INTRO_UD)
    return f"<Response>{_say(t) if t else ''}</Response>"


def twiml_efter_dial(form):
    """Efter ring-ud: blev der talt sammen -> laeg paa. Ellers telefonsvarer."""
    _AKTIVE_RINGUD.pop(_n8(form.get("From") or ""), None)
    status = (form.get("DialCallStatus") or "").lower()
    if status == "completed":
        # husk hvem der tog den (child-call'ets nummer slaas op senere)
        return "<Response><Hangup/></Response>"
    return twiml_telefonsvarer(optaget=(status == "busy"))


def twiml_telefonsvarer(optaget=False):
    if optaget:
        tekst = (db.get_meta("telefon_svarer_optaget") or STD_SVARER_OPTAGET)
    else:
        tekst = (db.get_meta("telefon_svarer") or STD_SVARER)
    tekst = tekst.replace("{firma}", _firma())
    return (f"<Response>{_say(tekst)}"
            f'<Record maxLength="120" playBeep="true" timeout="5" '
            f'recordingStatusCallback="{_url("optagelse?type=svarer")}" '
            f'recordingStatusCallbackEvent="completed" action="{_url("efter-svarer")}"/>'
            f"</Response>")


def twiml_efter_svarer():
    return f"<Response>{_say('Tak for din besked. Vi vender tilbage. Hej hej.')}<Hangup/></Response>"


# ---------- Twilio REST ----------

def _tw(method, sti, **kw):
    r = requests.request(method, f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/{sti}",
                         auth=(TWILIO_SID, TWILIO_TOKEN), timeout=30, **kw)
    if r.status_code >= 300 and method != "DELETE":
        raise RuntimeError(f"Twilio {r.status_code}: {r.text[:150]}")
    return r


def _hent_lyd(recording_url):
    r = requests.get(recording_url + ".mp3", auth=(TWILIO_SID, TWILIO_TOKEN), timeout=60)
    if not r.ok:
        raise RuntimeError(f"kunne ikke hente optagelse: HTTP {r.status_code}")
    return r.content


def _slet_optagelse(recording_sid):
    try:
        _tw("DELETE", f"Recordings/{recording_sid}.json")
    except Exception as e:
        print(f"[telefonnotat] kunne ikke slette optagelse {recording_sid}: {str(e)[:100]}", flush=True)


def _opkald(call_sid):
    try:
        return _tw("GET", f"Calls/{call_sid}.json").json()
    except Exception:
        return {}


def _besvaret_af(parent_sid):
    """Hvilken mobil tog den? (child-call med status completed)"""
    try:
        d = _tw("GET", "Calls.json", params={"ParentCallSid": parent_sid, "PageSize": 20}).json()
        for c in d.get("calls") or []:
            if c.get("status") == "completed":
                til = retell._norm_tlf(c.get("to"))[-8:]
                for m in mobiler():
                    if retell._norm_tlf(m["nummer"])[-8:] == til:
                        return m
    except Exception:
        pass
    return None


# ---------- udskrift + resume ----------

def _udskriv(lyd: bytes) -> str:
    """Udskrift UDEN ordliste-prompt: faar modellen stilhed, digter den ellers ud fra prompten
    ('registrer timer, planlaeg, materialer, Testvej 1...') - og det ligner en rigtig ordre."""
    f = io.BytesIO(lyd)
    f.name = "opkald.mp3"
    tr = telegram._client.audio.transcriptions.create(model=OPENAI_STT_MODEL, file=f, language="da")
    return (tr.text or "").strip()


_HALLUCINATION = ("tak fordi du så med", "tak for at du så med", "undertekster", "tekstet af",
                  "abonner", "www.", "amara.org", "musik", "♪")


def _reel_samtale(udskrift, sek):
    """Er der reelt sagt noget? Korte opkald og typiske 'stilheds-digte' filtreres fra."""
    u = (udskrift or "").strip().lower()
    if sek < 6:
        return False                     # 4 sek. kan ikke rumme en samtale
    if len(u) < 15:
        return False
    if any(h in u for h in _HALLUCINATION):
        return False
    ord_ = u.split()
    if sek < 15 and len(ord_) < 6:
        return False
    if len(set(ord_)) <= 2 and len(ord_) >= 4:   # "hallo hallo hallo hallo"
        return False
    return True


def _find_kunde(nummer):
    """Match mod ALLE telefonnumre i systemet: kundekort (alle telefonfelter) + kontaktpersoner."""
    k = None
    try:
        k = retell.find_kunde_ved_telefon(nummer)
    except Exception as e:
        print(f"[telefonnotat] kundeopslag fejlede: {str(e)[:120]}", flush=True)
    if k:
        return {"kundenummer": str(k.get("customer_number") or ""), "navn": k.get("customer_name") or "",
                "adresse": " ".join(x for x in (k.get("customer_address"), str(k.get("customer_postalcode") or ""),
                                               k.get("customer_city")) if x).strip(), "via": "kundekort"}
    # kontaktpersoner: fra telefon-indekset (bygges i baggrunden af scheduleren)
    try:
        n8 = retell._norm_tlf(nummer)[-8:]
        idx = json.loads(db.get_meta("telefon_indeks") or "{}")
        hit = idx.get(n8)
        if hit:
            return {"kundenummer": str(hit.get("kn") or ""), "navn": hit.get("kunde") or "",
                    "kontakt": hit.get("kontakt") or "", "adresse": hit.get("adresse") or "",
                    "via": "kontaktperson" if hit.get("kontakt") else "kundekort"}
    except Exception:
        pass
    return None


def byg_telefon_indeks():
    """Scheduler (hver 30. min): alle telefonnumre i systemet -> {8 cifre: {kn, kunde, kontakt, adresse}}.
    Kundekortets egne felter + kontaktpersoner (hentes med include=contacts)."""
    from . import ordrestyring as os_api
    idx = {}
    tlf_felter = ("telephone", "phone", "mobile", "telefon", "mobil")

    def _laeg(nr, kn, kunde, kontakt, adresse):
        d = retell._norm_tlf(nr)
        if len(d) >= 8:
            idx.setdefault(d[-8:], {"kn": kn, "kunde": kunde, "kontakt": kontakt, "adresse": adresse})

    try:
        debtors = os_api.all_debtors(force=True)
    except Exception as e:
        print(f"[telefon_indeks] kundeliste fejlede: {str(e)[:120]}", flush=True)
        return
    # 1) kundekortets egne numre
    for d in debtors:
        kn = str(d.get("customer_number") or "")
        adr = " ".join(x for x in (d.get("customer_address"), str(d.get("customer_postalcode") or ""),
                                   d.get("customer_city")) if x).strip()
        for k, v in d.items():
            if any(t in str(k).lower() for t in tlf_felter) and v:
                _laeg(v, kn, d.get("customer_name") or "", "", adr)
        for c in (d.get("contacts") or []) if isinstance(d.get("contacts"), list) else []:
            for k in ("telephone", "mobile"):
                if c.get(k):
                    _laeg(c[k], kn, d.get("customer_name") or "", c.get("name") or "", adr)
    # 2) kontaktpersoner - proev listen med include=contacts (et kald), ellers pr. kunde (begraenset)
    try:
        rows = os_api._data(os_api._req("GET", "/debtors", params={"include": "contacts", "pagesize": 500})) or []
        fandt = False
        for d in rows:
            cs = d.get("contacts")
            if isinstance(cs, list) and cs:
                fandt = True
                kn = str(d.get("customer_number") or "")
                for c in cs:
                    for k in ("telephone", "mobile"):
                        if c.get(k):
                            _laeg(c[k], kn, d.get("customer_name") or "", c.get("name") or "", "")
        if not fandt:
            # fallback: kun de 150 senest opdaterede kunder, saa det ikke tager evigheder
            for d in debtors[:150]:
                kn = d.get("customer_number")
                try:
                    for c in os_api.debtor_contacts(kn):
                        for k in ("telephone", "mobile"):
                            if c.get(k):
                                _laeg(c[k], str(kn), d.get("customer_name") or "", c.get("name") or "", "")
                except Exception:
                    continue
    except Exception as e:
        print(f"[telefon_indeks] kontakter fejlede: {str(e)[:120]}", flush=True)
    db.set_meta("telefon_indeks", json.dumps(idx, ensure_ascii=False))
    print(f"[telefon_indeks] {len(idx)} numre indekseret", flush=True)


def _resume(udskrift, kunde, type_):
    """GPT: kort, konkret resume som JSON."""
    from .agent import client
    kontekst = (f"Opkalderen er kendt: {kunde['navn']} (kundenr. {kunde['kundenummer']})"
                if kunde else "Opkalderen er IKKE kendt i systemet.")
    hvad = {"svarer": "en telefonsvarer-besked fra en kunde",
            "udgaaende": "en telefonsamtale hvor FIRMAET ringede kunden op"}.get(
                type_, "en telefonsamtale mellem firmaet og en kunde")
    r = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content":
                   "Du laver telefonnotater for et dansk haandvaerkerfirma. Skriv kort, konkret, paa dansk. "
                   "Find ALDRIG paa noget - staar det ikke ordret i udskriften, saa skriv det ikke. Er "
                   "udskriften bare 'hallo', 'hej', stilhed, en test eller uden reelt aerinde, saa tomt=true "
                   "og alt andet tomt. Svar KUN med JSON: "
                   '{"tomt": bool (ingen reel besked/aerinde), '
                   '"privat": bool (true hvis samtalen IKKE handler om firmaets arbejde - fx en ven, familie '
                   'eller kollega der bare sludrer om noget andet, uden opgave, kunde, tilbud, aftale eller '
                   'noget firmaet skal goere. Er der BARE en arbejdsting i samtalen, saa false), '
                   '"citat": "et ORDRET uddrag fra udskriften der viser aerindet (tom hvis intet)", '
                   '"aerinde": "hvad kunden vil, 1-2 linjer", '
                   '"aftalt": "hvad der blev aftalt (tid, pris, hvem goer hvad) eller tom", '
                   '"naeste_skridt": "det mest oplagte naeste skridt for firmaet, 1 linje", '
                   '"navn": "kundens navn hvis det naevnes ellers tom", '
                   '"adresse": "adresse hvis den naevnes ellers tom", '
                   '"telefon_naevnt": "andet telefonnummer hvis naevnt ellers tom", '
                   '"forslag": "opret_sag" | "planlaeg" | "sms" | "ring_tilbage" | "intet"}'},
                  {"role": "user", "content": f"Dette er {hvad}. {kontekst}\n\nUDSKRIFT:\n{udskrift[:6000]}"}])
    try:
        return json.loads(r.choices[0].message.content or "{}")
    except Exception:
        return {"aerinde": (r.choices[0].message.content or "")[:300]}


def _pn(nr):
    d = retell._norm_tlf(nr)
    return f"{d[:2]} {d[2:4]} {d[4:6]} {d[6:8]}" if len(d) == 8 else (nr or "skjult nummer")


def _min_sek(sek):
    try:
        s = int(sek)
        return f"{s // 60}:{s % 60:02d} min"
    except (ValueError, TypeError):
        return ""


def behandl_optagelse(params, type_):
    """Koeres i baggrundstraad naar Twilio melder at en optagelse er klar."""
    rec_sid = params.get("RecordingSid")
    rec_url = params.get("RecordingUrl")
    call_sid = params.get("CallSid")
    varighed = params.get("RecordingDuration")
    if not rec_url:
        return
    if db.get_meta(f"telefonnotat_{rec_sid}"):
        return   # Twilio kan melde to gange
    db.set_meta(f"telefonnotat_{rec_sid}", "1")

    opk = _opkald(call_sid)
    if type_ == "udgaaende":
        fra = params.get("_kunde") or ""          # kundens nummer (vi ringede op)
        besvaret = next((m for m in mobiler() if str(m.get("telegram_id")) == str(params.get("_tg"))), None)
    else:
        fra = opk.get("from") or params.get("From") or ""
        besvaret = _besvaret_af(call_sid) if type_ == "samtale" else None

    try:
        sek = int(varighed or 0)
    except (ValueError, TypeError):
        sek = 0
    try:
        lyd = _hent_lyd(rec_url)
    finally:
        _slet_optagelse(rec_sid)   # lyden gemmes ALDRIG - kun teksten
    udskrift = ""
    if sek >= 6:   # under 6 sek.: ingen udskrift overhovedet (sparer ogsaa AI-kald)
        try:
            udskrift = _udskriv(lyd)
        except Exception as e:
            telegram.send_leader(LEADER_GROUP_CHAT_ID,
                                 f"📞 Opkald fra {_pn(fra)} — kunne ikke skrive samtalen ud ({str(e)[:100]}).")
            return
    if not _reel_samtale(udskrift, sek):
        # Kort/tomt opkald: INGEN besked i Telegram (banneret har allerede vist hvem der ringede).
        # Gemmes kun i Opkald-loggen, saa det kan ses i Pilly.
        print(f"[telefonnotat] tom optagelse fra {fra}", flush=True)
        db.log_handling("", "Aura (telefon)", "system", "telefonnotat tomt", f"{_pn(fra)} ({type_})")
        try:
            db.gem_telefonsamtale(type_, retell._norm_tlf(fra), "", "", (besvaret or {}).get("navn") or "",
                                  int(varighed or 0), udskrift, "{}", "(kort opkald - ingen reel samtale)")
        except Exception:
            pass
        return

    kunde = _find_kunde(fra) if fra else None
    r = _resume(udskrift, kunde, type_)
    # Modellen skal kunne pege paa et ORDRET citat - kan den ikke, er aerindet digtet
    citat = (r.get("citat") or "").strip().lower()
    if citat and citat[:25] not in udskrift.lower():
        r["tomt"] = True
        r["aerinde"] = ""
    if r.get("tomt"):
        db.log_handling("", "Aura (telefon)", "system", "telefonnotat tomt", f"{_pn(fra)} ({type_})")
        return
    if r.get("privat"):
        # Privat snak (ven/familie/kollega-sludder): INGEN besked i Telegram, INTET i ordrestyring,
        # og udskriften gemmes IKKE - kun at der var et opkald (til Samtaler-fanen i Pilly).
        print(f"[telefonnotat] privat samtale fra {fra} - intet notat", flush=True)
        db.log_handling("", "Aura (telefon)", "system", "telefonnotat privat", f"{_pn(fra)} ({type_})")
        try:
            db.gem_telefonsamtale(type_, retell._norm_tlf(fra), "", "", (besvaret or {}).get("navn") or "",
                                  int(varighed or 0), "", "{}", "(privat samtale - intet noteret)")
        except Exception:
            pass
        _banner_slet(fra)
        return

    # ---- beskeden (HTML til Telegram = fed skrift; ren tekst gemmes til Auras hukommelse) ----
    hvem = (f"{kunde['navn']} (kundenr. {kunde['kundenummer']})" if kunde
            else (f"{r.get('navn')} — UKENDT nummer" if r.get("navn") else "ukendt nummer"))
    titel = {"svarer": "📞 Telefonsvarer-besked", "udgaaende": "📞 Telefonnotat (udgående)"}.get(
        type_, "📞 Telefonnotat") + f" — {hvem}"
    hvem_tog = (f" · ringet op af {besvaret['navn']}" if (besvaret and type_ == "udgaaende")
                else (f" · besvaret af {besvaret['navn']}" if besvaret else ""))
    # (label, tekst) - label vises FED i Telegram; tom label = almindelig linje
    dele = [("", f"<b>{_h(titel)}</b>"),
            ("", "☎️ " + _pn(fra) + (f" · {_min_sek(varighed)}" if varighed else "") + hvem_tog)]
    if kunde:
        dele.append(("", f"🧾 Kundenr.: {kunde['kundenummer']} (findes i ordrestyring"
                         + (f", kontakt: {kunde.get('kontakt')}" if kunde.get("kontakt") else "") + ")"))
        if kunde.get("adresse"):
            dele.append(("", f"📍 {kunde['adresse']}"))
    if r.get("adresse"):
        dele.append(("", f"📍 Nævnt i samtalen: {r['adresse']}"))
    if r.get("telefon_naevnt"):
        dele.append(("", f"📱 Andet nummer nævnt: {r['telefon_naevnt']}"))
    dele.append(("", ""))
    dele.append(("🗣 Kunden ville:", r.get("aerinde") or "(uklart)"))
    if r.get("aftalt"):
        dele.append(("🤝 Aftalt:", r["aftalt"]))
    if r.get("naeste_skridt"):
        dele.append(("➡️ Næste skridt:", r["naeste_skridt"]))
    dele.append(("", ""))
    forslag = r.get("forslag")
    if not kunde and fra and forslag != "intet":
        sp = ("Ukendt kunde: skal jeg sende ham en SMS, hvor han selv udfylder navn, adresse og "
              "email? Så opretter jeg ham som kunde, når han har svaret — og sagen bagefter. (svar ja)")
    elif forslag == "opret_sag":
        sp = "Skal jeg oprette sagen? (svar ja, eller ret mig)"
    elif forslag == "planlaeg":
        sp = "Skal jeg oprette sagen og planlægge den? (svar ja, eller ret mig)"
    elif forslag == "sms":
        sp = "Skal jeg sende kunden en SMS-bekræftelse? (svar ja, eller skriv teksten)"
    elif forslag == "ring_tilbage":
        sp = "Skal jeg lave en påmindelse om at ringe tilbage? (svar ja + tidspunkt)"
    else:
        sp = "Skal jeg gøre noget med det? (opret sag / SMS / påmindelse — eller 'nej')"
    dele.append(("", "❓ " + sp))
    # To versioner: HTML (fed skrift) til Telegram, ren tekst til hukommelse/dashboard
    html_linjer, plain_linjer = [], []
    for label, tekst_ in dele:
        if label:
            html_linjer.append(f"<b>{_h(label)}</b> {_h(tekst_)}")
            plain_linjer.append(f"{label} {tekst_}")
        elif tekst_.startswith("<b>"):
            html_linjer.append(tekst_)
            plain_linjer.append(titel)
        else:
            html_linjer.append(_h(tekst_))
            plain_linjer.append(tekst_)
    besked_html = "\n".join(html_linjer)
    besked = "\n".join(plain_linjer)
    try:
        db.gem_telefonsamtale(type_, retell._norm_tlf(fra), (kunde or {}).get("kundenummer") or "",
                              (kunde or {}).get("navn") or r.get("navn") or "",
                              (besvaret or {}).get("navn") or "", int(varighed or 0),
                              udskrift, json.dumps(r, ensure_ascii=False), besked)
    except Exception as e:
        print(f"[telefonnotat] kunne ikke gemme samtalen: {str(e)[:120]}", flush=True)

    # ---- modtagere: den der tog den + lederen/lederne ----
    modtagere = []
    if besvaret and besvaret.get("telegram_id"):
        modtagere.append(besvaret["telegram_id"])
    if LEADER_GROUP_CHAT_ID:
        modtagere.append(LEADER_GROUP_CHAT_ID)
    else:
        modtagere += [u["telegram_id"] for u in db.all_users() if u.get("rolle") == "pro"]
    for m in dict.fromkeys(str(x) for x in modtagere):
        try:
            try:
                telegram.send_message(m, besked_html, parse_mode="HTML")
            except Exception:
                telegram.send_message(m, besked)
            db.save_message(m, "assistant", besked)   # saa "ja, opret den" forstaas bagefter
        except Exception:
            pass
    _banner_slet(fra)   # 'ringer nu'-banneret har gjort sit arbejde - vaek med det
    db.log_handling("", "Aura (telefon)", "system", "telefonnotat sendt",
                    f"{hvem} · {_pn(fra)} · {type_}" + (f" · {besvaret['navn']}" if besvaret else ""))
    # udskriften gemmes kortvarigt, saa 'vis hele samtalen' er muligt
    db.set_meta(f"telefon_udskrift_{retell._norm_tlf(fra)[-8:]}", json.dumps(
        {"ts": time.time(), "tekst": udskrift[:4000], "type": type_}))


def start_behandling(params, type_):
    threading.Thread(target=behandl_optagelse, args=(dict(params), type_), daemon=True).start()

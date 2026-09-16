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


# ---------- indstillinger ----------

def _firma():
    return os.environ.get("FIRMA_NAVN", "Aura")


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


# ---------- TwiML (det Twilio skal goere) ----------

def twiml_indgaaende(form):
    """Foerste svar naar telefonen ringer: intro + ring ud til mobilerne (med optagelse)."""
    fra = form.get("From") or ""
    if not db.funktion_til("telefonnotat"):
        return f"<Response>{_say(STD_SVARER)}<Record maxLength=\"120\" playBeep=\"true\"/></Response>"
    mob = mobiler()
    intro = (db.get_meta("telefon_intro") or STD_INTRO).replace("{firma}", _firma())
    if not mob:
        return twiml_telefonsvarer()
    optag = "" if _privat(fra) else (
        f' record="record-from-answer-dual" recordingStatusCallback="{_url("optagelse?type=samtale")}"'
        f' recordingStatusCallbackEvent="completed"')
    caller = f' callerId="{escape(fra)}"' if fra.startswith("+") else ""
    numre = "".join(f"<Number>{m['nummer']}</Number>" for m in mob)
    return (f"<Response>{_say(intro)}"
            f'<Dial timeout="{RING_SEK}"{caller}{optag} action="{_url("efter-dial")}" method="POST">'
            f"{numre}</Dial></Response>")


def twiml_efter_dial(form):
    """Efter ring-ud: blev der talt sammen -> laeg paa. Ellers telefonsvarer."""
    status = (form.get("DialCallStatus") or "").lower()
    if status == "completed":
        # husk hvem der tog den (child-call'ets nummer slaas op senere)
        return "<Response><Hangup/></Response>"
    return twiml_telefonsvarer()


def twiml_telefonsvarer():
    tekst = (db.get_meta("telefon_svarer") or STD_SVARER).replace("{firma}", _firma())
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
    f = io.BytesIO(lyd)
    f.name = "opkald.mp3"
    tr = telegram._client.audio.transcriptions.create(
        model=OPENAI_STT_MODEL, file=f, language="da",
        prompt="Dansk telefonsamtale mellem en håndværker og en kunde om en opgave, adresse og tidspunkt. "
               + telegram._stt_prompt()[:600])
    return (tr.text or "").strip()


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
    hvad = "en telefonsvarer-besked fra en kunde" if type_ == "svarer" else "en telefonsamtale mellem firmaet og en kunde"
    r = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content":
                   "Du laver telefonnotater for et dansk haandvaerkerfirma. Skriv kort, konkret, paa dansk. "
                   "Find ikke paa noget - staar det ikke i udskriften, saa skriv det ikke. Svar KUN med JSON: "
                   '{"tomt": bool (ingen reel besked/aerinde), "aerinde": "hvad kunden vil, 1-2 linjer", '
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
    fra = opk.get("from") or params.get("From") or ""
    besvaret = _besvaret_af(call_sid) if type_ == "samtale" else None

    try:
        lyd = _hent_lyd(rec_url)
    finally:
        _slet_optagelse(rec_sid)   # lyden gemmes ALDRIG - kun teksten
    try:
        udskrift = _udskriv(lyd)
    except Exception as e:
        telegram.send_leader(LEADER_GROUP_CHAT_ID,
                             f"📞 Opkald fra {_pn(fra)} — kunne ikke skrive samtalen ud ({str(e)[:100]}).")
        return
    if len(udskrift) < 15:
        print(f"[telefonnotat] tom optagelse fra {fra} ignoreret", flush=True)
        db.log_handling("", "Aura (telefon)", "system", "telefonnotat tomt", f"{_pn(fra)} ({type_})")
        return

    kunde = _find_kunde(fra) if fra else None
    r = _resume(udskrift, kunde, type_)
    if r.get("tomt") and not (r.get("aerinde") or "").strip():
        db.log_handling("", "Aura (telefon)", "system", "telefonnotat tomt", f"{_pn(fra)} ({type_})")
        return

    # ---- beskeden ----
    hvem = (f"{kunde['navn']} (kundenr. {kunde['kundenummer']})" if kunde
            else (f"{r.get('navn')} — UKENDT nummer" if r.get("navn") else "ukendt nummer"))
    titel = ("📞 Telefonsvarer-besked" if type_ == "svarer" else "📞 Telefonnotat — samtale") + f" med {hvem}"
    linjer = [titel, f"Telefon: {_pn(fra)}" + (f" · {_min_sek(varighed)}" if varighed else "")
              + (f" · besvaret af {besvaret['navn']}" if besvaret else "")]
    if kunde:
        linjer.append(f"Kundenr.: {kunde['kundenummer']} (kunden FINDES i ordrestyring"
                      + (f", via kontaktperson {kunde.get('kontakt')}" if kunde.get("kontakt") else "") + ")")
        if kunde.get("adresse"):
            linjer.append(f"Adresse: {kunde['adresse']}")
    if r.get("adresse"):
        linjer.append(f"Adresse (nævnt i samtalen): {r['adresse']}")
    if r.get("telefon_naevnt"):
        linjer.append(f"Andet nummer nævnt: {r['telefon_naevnt']}")
    linjer.append("")
    linjer.append(f"Kunden ville: {r.get('aerinde') or '(uklart)'}")
    if r.get("aftalt"):
        linjer.append(f"Aftalt: {r['aftalt']}")
    if r.get("naeste_skridt"):
        linjer.append(f"Næste skridt: {r['naeste_skridt']}")
    linjer.append("")
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
    linjer.append("→ " + sp)
    besked = "\n".join(linjer)

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
            telegram.send_message(m, besked)
            db.save_message(m, "assistant", besked)   # saa "ja, opret den" forstaas bagefter
        except Exception:
            pass
    db.log_handling("", "Aura (telefon)", "system", "telefonnotat sendt",
                    f"{hvem} · {_pn(fra)} · {type_}" + (f" · {besvaret['navn']}" if besvaret else ""))
    # udskriften gemmes kortvarigt, saa 'vis hele samtalen' er muligt
    db.set_meta(f"telefon_udskrift_{retell._norm_tlf(fra)[-8:]}", json.dumps(
        {"ts": time.time(), "tekst": udskrift[:4000], "type": type_}))


def start_behandling(params, type_):
    threading.Thread(target=behandl_optagelse, args=(dict(params), type_), daemon=True).start()

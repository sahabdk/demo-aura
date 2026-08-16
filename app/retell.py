"""Retell-telefonagent (AI-Aura i røret): opkalder-opslag, besked til Dan, adresse-SMS.

Flow:
1) FØR samtalen kalder Retell /retell/{secret}/inbound med opkalderens nummer.
   Vi slår det op i ordrestyring og svarer med dynamiske variabler
   (kunde_fundet, navn, adresse, postnummer, by) som agenten bruger i samtalen.
2) EFTER samtalen sender Retell 'call_analyzed' til /retell/{secret}/webhook.
   Vi sender resumé til leder-gruppen i Telegram — og er kunden ukendt,
   får de en SMS (GatewayAPI) med link til adresse-siden /adr/{token}.
"""
import os
import re
import secrets as _secrets
import requests

from .config import LEADER_GROUP_CHAT_ID, APP_BASE_URL
from . import db, telegram
from . import ordrestyring as os_api

GATEWAYAPI_TOKEN = os.environ.get("GATEWAYAPI_TOKEN", "")
SMS_SENDER = (os.environ.get("SMS_SENDER", "VandtVandt") or "VandtVandt")[:11]


def _norm_tlf(nr):
    d = re.sub(r"\D", "", str(nr or ""))
    if d.startswith("0045"):
        d = d[4:]
    if d.startswith("45") and len(d) == 10:
        d = d[2:]
    return d


def find_kunde_ved_telefon(fra_nummer):
    """Find kunden i ordrestyring ud fra telefonnummer (tolerant for +45/mellemrum).
    Tjekker ALLE telefon-agtige felter paa kunden (telefon, mobil, faktura-mobil osv.),
    og proever igen med FRISK kundeliste hvis nummeret ikke findes i den cachede."""
    n = _norm_tlf(fra_nummer)
    if len(n) < 8:
        return None
    n8 = n[-8:]

    def _match(rows):
        for d in rows:
            for noegle, vaerdi in d.items():
                if not any(t in str(noegle).lower() for t in ("telephone", "phone", "mobile", "telefon", "mobil")):
                    continue
                k = _norm_tlf(vaerdi)
                if k and k[-8:] == n8:
                    return d
        return None

    hit = _match(os_api.all_debtors())
    if hit:
        return hit
    # ikke fundet i cachen: kunden kan vaere oprettet/rettet for nylig -> hent frisk liste
    try:
        return _match(os_api.all_debtors(force=True))
    except Exception as e:
        print(f"[retell] frisk kundeliste fejlede: {str(e)[:120]}", flush=True)
        return None


def inbound_vars(fra_nummer):
    """Dynamiske variabler til Retell-agentens åbning."""
    k = None
    try:
        k = find_kunde_ved_telefon(fra_nummer)
    except Exception as e:
        print(f"[retell] kundeopslag fejlede: {str(e)[:150]}", flush=True)
    if not k:
        return {"kunde_fundet": "nej", "navn": "", "adresse": "", "postnummer": "", "by": ""}
    return {"kunde_fundet": "ja",
            "kundenummer": str(k.get("customer_number") or ""),
            "navn": k.get("customer_name") or "",
            "adresse": k.get("customer_address") or "",
            "postnummer": str(k.get("customer_postalcode") or ""),
            "by": k.get("customer_city") or ""}


def send_sms(til, tekst):
    """SMS via GatewayAPI (token som brugernavn i basic auth). Uden token: dry-run i loggen."""
    if not db.funktion_til("sms"):
        print(f"[sms/deaktiveret] -> {til}: {tekst}", flush=True)
        return "deaktiveret"
    if db.get_meta("testtilstand") == "1":
        print(f"[sms/testtilstand] -> {til}: {tekst}", flush=True)
        return "dry-run"
    if not GATEWAYAPI_TOKEN:
        print(f"[sms/dry-run] -> {til}: {tekst}", flush=True)
        return "dry-run"
    msisdn = _norm_tlf(til)
    if len(msisdn) == 8:
        msisdn = "45" + msisdn
    r = requests.post("https://gatewayapi.com/rest/mtsms",
                      auth=(GATEWAYAPI_TOKEN, ""),
                      json={"sender": SMS_SENDER, "message": tekst,
                            "recipients": [{"msisdn": int(msisdn)}]},
                      timeout=20)
    if not r.ok:
        raise RuntimeError(f"GatewayAPI {r.status_code}: {r.text[:200]}")
    return "sent"


def haandter_afsluttet_opkald(payload):
    """'call_analyzed'-event fra Retell: besked til lederen + evt. adresse-SMS til kunden."""
    call = payload.get("call") or {}
    fra = call.get("from_number") or ""
    analysis = call.get("call_analysis") or {}
    custom = analysis.get("custom_analysis_data") or {}
    resume = custom.get("call_summary") or analysis.get("call_summary") or "(intet resumé)"
    navn = (custom.get("kunde_navn") or "").strip()
    akut = bool(custom.get("akut"))
    dyn = call.get("retell_llm_dynamic_variables") or {}
    kendt = str(dyn.get("kunde_fundet")) == "ja"
    if kendt and not navn:
        navn = dyn.get("navn") or ""
    adresse = " ".join(x for x in (dyn.get("adresse"), dyn.get("postnummer"), dyn.get("by")) if x)

    # TOMT OPKALD-FILTER: lagde kunden paa uden reelt at sige noget, og er der hverken
    # navn, aerinde eller noget akut - saa forstyr IKKE lederen og send INGEN sms.
    transcript = call.get("transcript") or ""
    kunde_ord = " ".join(l.split(":", 1)[1] for l in transcript.splitlines()
                         if l.strip().lower().startswith("user:"))
    tomt = (not akut and not navn.strip()
            and len(kunde_ord.strip()) < 15
            and not (custom.get("aerinde") or "").strip())
    # RELEVANS-VAGT: sagde kunden noget, men uden reelt aerinde ("det var ikke noget
    # alligevel", forkert nummer, fortrudt) - saa vurderer AI'en indholdet. Fejler
    # vurderingen, sendes beskeden hellere en gang for meget (fail-open).
    if not tomt and not akut:
        try:
            from .agent import client
            from .config import OPENAI_MODEL
            svar = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content":
                    "Et telefonopkald til et el/vvs-firma er slut.\n"
                    f"Resumé: {resume}\nKundens egne ord: {kunde_ord[:500]}\n\n"
                    "Skal firmaet foretage sig NOGET (ringe tilbage, oprette sag, "
                    "notere en besked, sende en tekniker)? Svar KUN 'ja' eller 'nej'. "
                    "Svar 'nej' hvis kunden fortrød, tog fejl af nummeret, ikke havde "
                    "noget ærinde alligevel, eller opkaldet ellers er uden indhold."}],
            )
            if "nej" in (svar.choices[0].message.content or "").strip().lower()[:6]:
                tomt = True
        except Exception as e:
            print(f"[retell] relevans-vagt fejlede (sender alligevel): {str(e)[:120]}", flush=True)
    if tomt:
        try:
            db.log_handling("", "AI-Aura (telefon)", "system", "tomt opkald ignoreret",
                            f"{fra or 'skjult nummer'}: {(resume or 'lagde paa uden besked')[:150]}")
        except Exception:
            pass
        print(f"[retell] tomt opkald fra {fra} ignoreret (ingen besked/aerinde)", flush=True)
        return

    linjer = ["🚨 AKUT — telefonbesked fra AI-Aura:" if akut else "📞 Telefonbesked fra AI-Aura:",
              f"Kunde: {navn or 'ukendt navn'}",
              f"Telefon: {fra or 'skjult nummer'}"]
    if kendt and dyn.get("kundenummer"):
        linjer.append(f"Kundenr.: {dyn.get('kundenummer')} (kunden FINDES i ordrestyring)")
    if adresse:
        linjer.append(f"Adresse: {adresse}")
    elif kendt:
        linjer.append("Adresse: (kendt kunde, se ordrestyring)")
    linjer.append(f"Besked: {resume}")

    if not adresse:
        adresse_fra_samtalen = (custom.get("adresse") or "").strip()
        if adresse_fra_samtalen:
            adresse = adresse_fra_samtalen
            linjer.insert(3, f"Adresse (oplyst i samtalen): {adresse}")

    sms_status = None
    # Adresse-SMS kun naar den giver mening: ukendt nummer OG adressen mangler stadig
    if not kendt and fra and not adresse:
        try:
            token = _secrets.token_urlsafe(16)
            db.create_adr_request(token, fra, navn)
            link = f"{(APP_BASE_URL or '').rstrip('/')}/adr/{token}"
            sms_tekst = (db.get_meta("skabelon_sms_adresse")
                         or "Tak for dit opkald til Vandt & Vandt. Skriv venligst din adresse "
                            "her, så vi har den helt rigtigt: {link}")
            ret = send_sms(fra, sms_tekst.replace("{link}", link))
            sms_status = ret
            if ret == "sent":
                linjer.append("📱 Ukendt nummer → kunden har fået SMS-link til at skrive sin adresse.")
            else:
                linjer.append(f"📱 Adresse-SMS blev IKKE sendt ({ret}) — link: {link}")
        except Exception as e:
            sms_status = f"fejlede: {str(e)[:120]}"
            linjer.append(f"⚠️ Adresse-SMS kunne ikke sendes ({sms_status}).")
            print(f"[retell] sms-fejl: {str(e)[:200]}", flush=True)

    telegram.send_leader(LEADER_GROUP_CHAT_ID, "\n".join(linjer))
    try:
        db.log_handling("", "AI-Aura (telefon)", "system", "telefonbesked modtaget",
                        f"{navn or 'ukendt'} / {fra}: {resume[:200]}")
    except Exception:
        pass
    print(f"[retell] opkald behandlet: kendt={kendt}, sms={sms_status}", flush=True)


# ---------- adresse-portal (/adr/{token}) ----------

_ADR_HTML = """<!doctype html><html lang="da"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vandt & Vandt — din adresse</title>
<style>body{{font-family:sans-serif;max-width:26rem;margin:2rem auto;padding:0 1rem}}
input{{width:100%;padding:.6rem;margin:.3rem 0 1rem;border:1px solid #ccc;border-radius:6px}}
button{{background:#1a7f37;color:#fff;border:0;padding:.7rem 1.4rem;border-radius:6px;font-size:1rem}}
</style></head><body><h2>Vandt & Vandt</h2><p>{besked}</p>{form}</body></html>"""

_FELT_TEKST = {"navn": "Navn", "adresse": "Adresse (vej og nr.)", "postnr": "Postnummer",
               "by": "By", "email": "Email"}


def _adr_form(felter, navn=""):
    """Byg formularen dynamisk - stamdata-links viser KUN de felter der mangler."""
    dele = []
    for f in felter:
        typ = "email" if f == "email" else "text"
        vaerdi = navn if f == "navn" else ""
        dele.append(f'<label>{_FELT_TEKST.get(f, f)}</label>'
                    f'<input name="{f}" type="{typ}" value="{vaerdi}" required>')
    return '<form method="post">' + "".join(dele) + '<button type="submit">Send</button></form>'


def _adr_felter(r):
    felter = [f for f in (r.get("felter") or "").split(",") if f]
    return felter or ["navn", "adresse", "postnr", "by"]


def adr_side(token):
    r = db.get_adr_request(token)
    if not r:
        return _ADR_HTML.format(besked="Linket er ugyldigt eller udløbet.", form="")
    if r.get("status") == "done":
        return _ADR_HTML.format(besked="Tak — vi har allerede modtaget dine oplysninger. 👍", form="")
    if r.get("kundenummer"):
        besked = "Tak fordi du valgte os! Vi mangler et par oplysninger i vores kartotek — udfyld dem venligst herunder."
    else:
        besked = "Tak for dit opkald! Skriv din adresse herunder, så vi har den helt rigtigt."
    return _ADR_HTML.format(besked=besked, form=_adr_form(_adr_felter(r), navn=(r.get("navn") or "")))


def adr_submit(token, form):
    r = db.get_adr_request(token)
    if not r or r.get("status") == "done":
        return adr_side(token)
    felter = _adr_felter(r)
    svar = {f: (form.get(f) or "").strip() for f in felter}
    navn = svar.get("navn") or (r.get("navn") or "")
    vist = ", ".join(f"{_FELT_TEKST.get(f, f)}: {v}" for f, v in svar.items() if v)

    if r.get("kundenummer"):
        # STAMDATA-flow: skriv de udfyldte felter direkte ind paa kundekortet i ordrestyring
        try:
            aendringer = {f: v for f, v in svar.items() if v and f in ("adresse", "postnr", "by", "email")}
            if aendringer:
                os_api.update_debtor(r["kundenummer"], **aendringer)
            db.mark_adr_done(token, navn, vist)
            telegram.send_leader(LEADER_GROUP_CHAT_ID,
                                     f"📇 Kundekort udfyldt af kunden selv (kunde {r['kundenummer']}"
                                     f"{' - ' + navn if navn else ''}):\n{vist}\n→ opdateret i ordrestyring")
            db.log_handling("", "Aura (stamdata)", "system", "kundekort opdateret af kunde",
                            f"kunde {r['kundenummer']}: {vist}")
        except Exception as e:
            print(f"[stamdata] sync-fejl: {str(e)[:200]}", flush=True)
            db.log_handling("", "Aura (stamdata)", "system", "stamdata sync-fejl",
                            f"kunde {r.get('kundenummer')}: {str(e)[:150]}")
            return _ADR_HTML.format(besked="Noget gik galt — prøv venligst igen om lidt.",
                                    form=_adr_form(felter, navn))
        return _ADR_HTML.format(besked="Tak! Vi har modtaget dine oplysninger. 👍", form="")

    adresse = f"{svar.get('adresse', '')}, {svar.get('postnr', '')} {svar.get('by', '')}".strip(", ")
    db.mark_adr_done(token, navn, adresse)
    telegram.send_leader(LEADER_GROUP_CHAT_ID,
                              f"🏠 Adresse modtaget fra telefonkunde:\n{navn}\n{adresse}\nTelefon: {r.get('telefon')}")
    try:
        db.log_handling("", "AI-Aura (telefon)", "system", "adresse modtaget",
                        f"{navn}: {adresse} ({r.get('telefon')})")
    except Exception:
        pass
    return _ADR_HTML.format(besked="Tak! Vi har modtaget din adresse. 👍", form="")

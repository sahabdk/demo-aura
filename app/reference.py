"""Referencenummer-portal: scanning, kunde-portal (HTML) og tilbageskrivning.

Flow: en planlagt scanning finder de store kunders åbne sager uden referencenummer,
opretter en anmodning med et unikt token og mailer kunden et link. Kunden indtaster
referencen i portalen, som skrives tilbage på sagen (yourref) og giver lederen besked.
"""
import html
import logging
import secrets
from datetime import datetime

from . import config, db, telegram
from . import ordrestyring as os_api
from .email import send_reference_request, FIRMA

log = logging.getLogger("aura.reference")


# ---------- planlagt scanning ----------

def scan_and_notify():
    """Kører planlagt: find sager uden reference hos de store kunder og mail dem.
    Returnerer diagnostik pr. kunde, så vi kan se hvor evt. sager filtreres fra."""
    stats = {"kunder": {}}
    if not config.REF_CUSTOMERS or not config.APP_BASE_URL:
        return stats

    for cn in config.REF_CUSTOMERS:
        s = {"sager": 0, "uden_ref": 0, "aabne_uden_ref": 0, "email": False,
             "oprettet": 0, "fejl": None}
        try:
            debtor = os_api.get_debtor(cn) or {}
            cases = os_api.cases_for_customer(cn)
        except Exception as e:
            s["fejl"] = str(e)[:80]
            stats["kunder"][cn] = s
            log.exception("ref-scan: kunne ikke hente kunde %s", cn)
            continue

        email = (debtor.get("customer_email") or "").strip()
        navn = debtor.get("customer_name") or "kunde"
        adresse = (f"{debtor.get('customer_address','')} {debtor.get('customer_postalcode','')} "
                   f"{debtor.get('customer_city','')}").strip()
        s["email"] = bool(email)
        s["sager"] = len(cases)

        for case in cases:
            nr = case.get("case_number")
            if not nr:
                continue
            rec = db.get_ref_request_by_case(nr)

            # Referencen er kommet ind (portal eller direkte i ordrestyring) -> luk anmodningen
            if os_api.has_reference(case):
                if rec and rec["status"] == "pending":
                    db.mark_ref_done_by_case(nr, (case.get("yourref") or "").strip())
                continue
            s["uden_ref"] += 1
            if os_api.is_closed(case):
                continue
            s["aabne_uden_ref"] += 1
            if not email:
                continue

            sag_tekst = f"Sag {nr}: {case.get('description') or 'opgave'} – {adresse}".strip(" –")
            if not rec:
                token = secrets.token_urlsafe(16)
                db.create_ref_request(token, nr, cn)
                _send(token, navn, email, sag_tekst, reminder=False)
                s["oprettet"] += 1
            elif rec["status"] == "pending" and _should_remind(rec):
                _send(rec["token"], navn, email, sag_tekst, reminder=True)

        stats["kunder"][cn] = s
    return stats


def _send(token, navn, email, sag_tekst, reminder):
    link = f"{config.APP_BASE_URL}/ref/{token}"
    try:
        send_reference_request(email, navn, sag_tekst, link, reminder=reminder)
        db.mark_ref_mailed(token)
    except Exception:
        log.exception("ref-scan: kunne ikke sende mail (token %s)", token)


def _should_remind(rec):
    if (rec.get("antal_mails") or 0) >= config.REF_MAX_MAILS:
        return False
    last = rec.get("sidste_mail_at")
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False
    return (datetime.utcnow() - last_dt).days >= config.REF_REMINDER_DAGE


# ---------- kunde-portal (HTML) ----------

def _page(indhold: str, titel: str = "Referencenummer") -> str:
    return f"""<!doctype html><html lang="da"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(titel)} · {html.escape(FIRMA)}</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f3f4f6;
       margin:0;padding:24px;color:#111}}
  .kort{{max-width:480px;margin:32px auto;background:#fff;border-radius:14px;padding:28px;
        box-shadow:0 6px 24px rgba(0,0,0,.08)}}
  h1{{font-size:20px;margin:0 0 4px}} .firma{{color:#6b7280;font-size:14px;margin-bottom:20px}}
  .sag{{background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:14px;margin:16px 0;
       font-size:15px;color:#374151}}
  label{{display:block;font-weight:600;margin:18px 0 6px}}
  input[type=text]{{width:100%;box-sizing:border-box;padding:13px;font-size:17px;border:1px solid #d1d5db;
       border-radius:10px}}
  button{{margin-top:18px;width:100%;padding:14px;font-size:17px;font-weight:600;color:#fff;
        background:#2563eb;border:0;border-radius:10px;cursor:pointer}}
  .ok{{color:#047857}} .fejl{{color:#b91c1c;font-size:14px;margin-top:8px}}
</style></head><body><div class="kort">
<div class="firma">{html.escape(FIRMA)}</div>
{indhold}
</div></body></html>"""


def portal_page(token: str) -> str:
    rec = db.get_ref_request(token)
    if not rec:
        return _page("<h1>Linket er ugyldigt</h1><p>Linket er ukendt eller udløbet. "
                     "Kontakt os hvis du mener det er en fejl.</p>")
    if rec["status"] == "done":
        return _page(f"<h1 class='ok'>Tak!</h1><p>Vi har allerede modtaget referencenummeret "
                     f"<b>{html.escape(rec.get('reference') or '')}</b> for denne opgave.</p>")

    case = os_api.get_case(rec["case_number"]) or {}
    debtor = os_api.get_debtor(rec["customer_number"]) or {}
    adresse = (f"{debtor.get('customer_address','')} {debtor.get('customer_postalcode','')} "
               f"{debtor.get('customer_city','')}").strip()
    besk = case.get("description") or "opgave"
    sag = html.escape(f"Sag {rec['case_number']}: {besk}" + (f" – {adresse}" if adresse else ""))
    return _page(
        "<h1>Indtast referencenummer</h1>"
        "<p>Vi mangler et referencenummer for at kunne fakturere denne opgave korrekt.</p>"
        f"<div class='sag'>{sag}</div>"
        f"<form method='post' action='/ref/{html.escape(token)}'>"
        "<label for='reference'>Referencenummer</label>"
        "<input type='text' id='reference' name='reference' autocomplete='off' required "
        "placeholder='Fx 12345'>"
        "<button type='submit'>Send referencenummer</button></form>")


def submit_reference(token: str, reference: str) -> str:
    rec = db.get_ref_request(token)
    if not rec:
        return _page("<h1>Linket er ugyldigt</h1><p>Linket er ukendt eller udløbet.</p>")
    if rec["status"] == "done":
        return _page("<h1 class='ok'>Tak!</h1><p>Referencenummeret er allerede modtaget.</p>")

    reference = (reference or "").strip()
    if not reference:
        return _page("<h1>Indtast referencenummer</h1>"
                     f"<form method='post' action='/ref/{html.escape(token)}'>"
                     "<label for='reference'>Referencenummer</label>"
                     "<input type='text' id='reference' name='reference' required>"
                     "<div class='fejl'>Feltet må ikke være tomt.</div>"
                     "<button type='submit'>Send referencenummer</button></form>")

    # Skriv referencen tilbage på sagen (yourref)
    try:
        os_api.update_case(rec["case_number"], reference=reference)
    except Exception:
        log.exception("ref-portal: kunne ikke skrive reference på sag %s", rec["case_number"])
        return _page("<h1>Beklager</h1><p>Der opstod en fejl. Prøv igen om lidt, "
                     "eller kontakt os.</p>")

    db.mark_ref_done(token, reference)
    _notify_leder(rec, reference)
    return _page(f"<h1 class='ok'>Tak!</h1><p>Vi har modtaget referencenummeret "
                 f"<b>{html.escape(reference)}</b>. Du kan lukke denne side.</p>")


def scan_and_links(maks=20):
    """Kør scanning nu og returnér en oversigt med portal-links (til test fra Telegram)."""
    if not config.REF_CUSTOMERS:
        return "Ingen store kunder er konfigureret endnu (sæt REF_CUSTOMERS i Railway)."
    if not config.APP_BASE_URL:
        return "APP_BASE_URL mangler — sæt den i Railway, så jeg kan lave portal-links."
    stats = {}
    try:
        stats = scan_and_notify()
    except Exception:
        log.exception("manuel ref-scan fejlede")
    pend = db.pending_ref_requests()
    if pend:
        linjer = [f"- Sag {p['case_number']}: {config.APP_BASE_URL}/ref/{p['token']}" for p in pend[:maks]]
        ekstra = f"\n… og {len(pend) - maks} mere" if len(pend) > maks else ""
        return f"🔗 Sager der mangler referencenummer ({len(pend)}):\n" + "\n".join(linjer) + ekstra

    # Intet oprettet -> vis diagnostik så vi kan se hvorfor
    diag = []
    for cn, s in (stats.get("kunder") or {}).items():
        if s.get("fejl"):
            diag.append(f"- {cn}: FEJL {s['fejl']}")
        else:
            diag.append(f"- {cn}: {s['sager']} sager · {s['aabne_uden_ref']} åbne uden ref · "
                        f"email={'ja' if s['email'] else 'nej'}")
    if not diag:
        return ("Ingen kunder blev scannet. Tjek at REF_CUSTOMERS er sat i Railway "
                f"(lige nu: {config.REF_CUSTOMERS or 'tom'}).")
    return ("Ingen portal-links oprettet. Diagnostik (kunde: antal sager · åbne uden ref · har email):\n"
            + "\n".join(diag))


def _notify_leder(rec, reference):
    if not config.LEADER_GROUP_CHAT_ID:
        return
    try:
        navn = (os_api.get_debtor(rec["customer_number"]) or {}).get("customer_name") or rec["customer_number"]
    except Exception:
        navn = rec["customer_number"]
    try:
        telegram.send_message(
            config.LEADER_GROUP_CHAT_ID,
            f"✅ Referencenummer modtaget: sag {rec['case_number']} → {reference} (fra {navn})")
    except Exception:
        log.exception("ref-portal: kunne ikke notificere leder")

"""Eskalerende betalingspåmindelser (1./2./3. niveau) + reference-mails.

Afsendelse (i prioriteret rækkefølge):
  1) RESEND_API_KEY sat -> send via Resend's HTTPS-API (virker fra Railway, der blokerer SMTP).
  2) ellers SMTP_HOST sat -> send via SMTP (kun hvis udgående SMTP er tilladt, fx Railway Pro).
  3) ellers 'dry-run' -> log kun, send intet.
"""
import os
import smtplib
import requests
from email.mime.text import MIMEText

FIRMA = os.environ.get("FIRMA_NAVN", "Vandt og Vandt ApS")
# Betalingsinstruktion der kommer med i hver rykker (sæt fx bankkonto/FI-nr. i Railway)
PAYMENT_INFO = os.environ.get("PAYMENT_INFO", "")

TEMPLATES = {
    1: ("Betalingspåmindelse",
        "Vi kan se, at du har en forfalden, ubetalt faktura hos os. "
        "Vi beder dig venligt betale den hurtigst muligt. "
        "Har du allerede betalt, så se bort fra denne besked."),
    2: ("2. rykker — betalingspåmindelse",
        "Vi har tidligere mindet dig om en forfalden faktura, som vi endnu ikke har modtaget "
        "betaling for. Vi beder dig betale senest 8 dage fra dags dato."),
    3: ("Sidste rykker før inkasso",
        "Dette er sidste påmindelse vedrørende din forfaldne faktura. Modtager vi ikke betaling "
        "senest 8 dage fra dags dato, oversendes sagen til inkasso, hvilket kan medføre gebyrer."),
}


def send_payment_reminder(to_email: str, navn: str, level: int, beloeb: str = None,
                          forfald: str = None, dage_forsinket: int = None, fakturanr=None) -> str:
    """Sender en eskalerende rykker med fakturadetaljer + betalingsinfo. Returnerer 'sent'/'dry-run'."""
    subject, body = TEMPLATES.get(level, TEMPLATES[1])
    linjer = [f"Kære {navn},", "", body]

    detaljer = []
    if fakturanr:
        detaljer.append(f"Faktura: {fakturanr}")
    if beloeb:
        detaljer.append(f"Skyldigt beløb: {beloeb} kr.")
    if forfald:
        f = f"Forfaldsdato: {forfald}"
        if dage_forsinket and dage_forsinket > 0:
            f += f" ({dage_forsinket} dage forsinket)"
        detaljer.append(f)
    if detaljer:
        linjer += [""] + detaljer

    if PAYMENT_INFO:
        linjer += ["", PAYMENT_INFO]
    linjer += ["", "Venlig hilsen", FIRMA]
    return _send(to_email, subject, "\n".join(linjer))


def _send(to_email: str, subject: str, text: str) -> str:
    """Sender en mail. Resend-API foretrækkes (virker fra Railway); ellers SMTP; ellers dry-run."""
    try:   # TESTTILSTAND (Pilly-dashboardet): log i stedet for at sende
        from . import db as _db
        if _db.get_meta("testtilstand") == "1":
            print(f"[EMAIL/testtilstand] -> {to_email} | {subject}\n{text[:300]}", flush=True)
            return "dry-run"
    except Exception:
        pass
    if os.environ.get("RESEND_API_KEY"):
        return _send_resend(to_email, subject, text)

    host = os.environ.get("SMTP_HOST")
    if not host:
        print(f"[EMAIL/dry-run] -> {to_email} | {subject}\n{text}")
        return "dry-run"
    port = int(os.environ.get("SMTP_PORT", 587))
    user, pw = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    msg = MIMEText(text)
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM", user)
    msg["To"] = to_email
    if port == 465:   # implicit SSL
        with smtplib.SMTP_SSL(host, port, timeout=20) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:             # STARTTLS (fx 587)
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
    return "sent"


def _send_resend(to_email: str, subject: str, text: str) -> str:
    """Send via Resend's HTTPS-API (port 443 — virker fra Railway hvor SMTP er blokeret)."""
    frm = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                 "Content-Type": "application/json"},
        json={"from": frm, "to": [to_email], "subject": subject, "text": text},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Resend {r.status_code}: {r.text[:200]}")
    return "sent"


def send_reference_request(to_email: str, navn: str, sag_tekst: str, link: str,
                           reminder: bool = False) -> str:
    """Beder kunden om at indtaste referencenummer via portal-link. Returnerer 'sent'/'dry-run'."""
    if reminder:
        subject = "Påmindelse: vi mangler dit referencenummer"
        indledning = ("Vi mangler stadig et referencenummer til en opgave, vi har udført for jer. "
                      "Vi beder dig venligt indtaste det, så vi kan fakturere korrekt.")
    else:
        subject = "Vi mangler et referencenummer"
        indledning = ("Vi har registreret en opgave for jer, men mangler et referencenummer for at "
                      "kunne fakturere korrekt. Du kan nemt indtaste det via linket nedenfor.")
    text = (f"Kære {navn},\n\n{indledning}\n\n"
            f"Opgave: {sag_tekst}\n\n"
            f"Indtast referencenummer her:\n{link}\n\n"
            f"På forhånd tak.\n\nVenlig hilsen\n{FIRMA}")
    return _send(to_email, subject, text)

"""Eskalerende betalingspåmindelser (1./2./3. niveau).

Brug SMTP (fx Gmail med app-password) eller skift til et udbyder-API.
Sætter du SMTP_* miljøvariabler ind, sender den rigtigt; ellers logger den bare.
"""
import os
import smtplib
from email.mime.text import MIMEText

FIRMA = os.environ.get("FIRMA_NAVN", "Vandt og Vandt ApS")

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


def send_payment_reminder(to_email: str, navn: str, level: int,
                          beloeb: str = None, forfald: str = None) -> str:
    """Sender en eskalerende rykker. Returnerer 'sent' ved rigtig afsendelse eller
    'dry-run' hvis SMTP ikke er konfigureret (så kalderen ved at intet blev sendt)."""
    subject, body = TEMPLATES.get(level, TEMPLATES[1])
    detaljer = ""
    if beloeb:
        detaljer = f"\n\nSkyldigt beløb: {beloeb} kr."
        if forfald:
            detaljer += f" (forfald {forfald})"
        detaljer += "."
    text = f"Kære {navn},\n\n{body}{detaljer}\n\nVenlig hilsen\n{FIRMA}"
    return _send(to_email, subject, text)


def _send(to_email: str, subject: str, text: str) -> str:
    """Sender en mail via SMTP. Returnerer 'sent' eller 'dry-run' hvis SMTP ikke er sat op."""
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

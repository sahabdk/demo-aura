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

    host = os.environ.get("SMTP_HOST")
    if not host:
        print(f"[EMAIL/dry-run] -> {to_email} | {subject}\n{text}")
        return "dry-run"

    msg = MIMEText(text)
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", ""))
    msg["To"] = to_email
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587))) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    return "sent"

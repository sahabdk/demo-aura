"""Knap-menu til lederen (pro): nye ordrer, detaljer-udfold og tildel — via Telegram inline-knapper."""
import re
from datetime import datetime
from . import ordrestyring as os_api
from . import db
from . import telegram


def _kundenavn(customer_number):
    for d in os_api.all_debtors():
        if str(d.get("customer_number")) == str(customer_number):
            return d.get("customer_name")
    try:
        return (os_api.get_debtor(customer_number) or {}).get("customer_name")
    except Exception:
        return None


def _kort(case):
    nr = case.get("case_number")
    kunde = _kundenavn(case.get("customer_number")) or "ukendt"
    desc = (case.get("description") or "")[:50]
    return f"Sag {nr} · {kunde} · {desc}".rstrip(" ·")


def _ordre_knapper(nr):
    return [[("📋 Detaljer", f"det:{nr}"), ("👤 Tildel", f"tildel:{nr}")]]


# ---------- hovedmenu ----------

def send_main_menu(chat_id):
    telegram.send_buttons(chat_id, "📋 Hvad vil du se?", [
        [("🆕 Nye ordrer", "nye")],
        [("📋 Dagens ordrer", "dagens")],
        [("🧾 Forfaldne fakturaer", "fakt")],
        [("📅 Dagens aftaler", "aft")],
    ])


# ---------- nye / dagens ordrer ----------

def _vis_ordre_liste(chat_id, cases, header, tom):
    if not cases:
        telegram.send_message(chat_id, tom)
        return
    if len(cases) > 20:
        # For mange til knapper -> kompakt liste + antal (ingen spam)
        linjer = [f"- {_kort(c)}" for c in cases[:40]]
        ekstra = f"\n… og {len(cases) - 40} flere" if len(cases) > 40 else ""
        telegram.send_message(
            chat_id,
            f"{header}: {len(cases)} ordrer — det er for mange til at vise med knapper. Her er overblikket:\n"
            + "\n".join(linjer) + ekstra
            + "\n\nSig fx \"vis sag 124\" for detaljer og tildeling på en bestemt ordre.")
        return
    telegram.send_message(chat_id, f"{header}: {len(cases)}")
    for case in cases:
        telegram.send_buttons(chat_id, _kort(case), _ordre_knapper(case.get("case_number")))


def vis_nye_ordrer(chat_id, telegram_id):
    since = db.get_last_seen_order(telegram_id)
    cases = os_api.new_cases(since)
    _vis_ordre_liste(chat_id, cases, "🆕 Nye ordrer siden sidst", "Ingen nye ordrer siden sidst. 👍")
    if cases:
        newest = max((int(c.get("created_at") or 0) for c in cases), default=since)
        db.set_last_seen_order(telegram_id, newest + 1)


def vis_dagens_ordrer(chat_id, telegram_id):
    cases = os_api.today_cases()
    _vis_ordre_liste(chat_id, cases, "📋 Dagens ordrer", "Ingen ordrer i dag.")


# ---------- detaljer ----------

def _detalje_tekst(case_number):
    case = os_api.get_case(case_number) or {}
    kunde = _kundenavn(case.get("customer_number")) or "ukendt"
    try:
        debtor = os_api.get_debtor(case.get("customer_number")) or {}
    except Exception:
        debtor = {}
    ansvarlig = os_api.user_name(case.get("main_technician")) or "ingen"
    adresse = f"{debtor.get('customer_address','')} {debtor.get('customer_postalcode','')} {debtor.get('customer_city','')}".strip()
    linjer = [
        f"📋 Sag {case_number}",
        f"Kunde: {kunde}",
        f"Adresse: {adresse}",
        f"Telefon: {debtor.get('customer_telephone','') or '-'}",
        f"Beskrivelse: {case.get('description','') or '-'}",
    ]
    if case.get("remarks"):
        linjer.append(f"Bemærkninger: {case.get('remarks')}")
    if case.get("work_done"):
        linjer.append(f"Færdiggjort: {case.get('work_done')}")
    linjer.append(f"Ansvarlig: {ansvarlig}")
    return "\n".join(linjer)


def vis_detaljer(chat_id, message_id, case_number):
    telegram.edit_message(chat_id, message_id, _detalje_tekst(case_number), [
        [("🔼 Skjul", f"skjul:{case_number}"), ("👤 Tildel", f"tildel:{case_number}")],
    ])


def skjul_detaljer(chat_id, message_id, case_number):
    case = os_api.get_case(case_number) or {"case_number": case_number}
    telegram.edit_message(chat_id, message_id, _kort(case), _ordre_knapper(case_number))


# ---------- tildel ----------

def vis_tildel(chat_id, message_id, case_number):
    rows, raekke = [], []
    for u in os_api.users():
        navn = (u.get("fullName") or u.get("first_name") or u.get("init") or str(u.get("id")))
        if not navn:
            continue
        raekke.append((navn[:20], f"sat:{case_number}:{u.get('id')}"))
        if len(raekke) == 2:
            rows.append(raekke)
            raekke = []
        if len(rows) >= 8:
            break
    if raekke:
        rows.append(raekke)
    rows.append([("↩️ Tilbage", f"skjul:{case_number}")])
    telegram.edit_message(chat_id, message_id, f"Hvem skal have sag {case_number}?", rows)


def saet_ansvarlig(chat_id, message_id, case_number, tech_id):
    os_api.assign_case(case_number, tech_id)
    navn = os_api.user_name(tech_id) or "medarbejder"
    telegram.edit_message(chat_id, message_id, f"✅ Sag {case_number} tildelt {navn}.",
                          [[("📋 Detaljer", f"det:{case_number}")]])


# ---------- info-knapper ----------

def vis_forfaldne(chat_id):
    from .tools import forfaldne_fakturaer
    fakturaer = forfaldne_fakturaer({}, {}).get("fakturaer", [])
    if not fakturaer:
        telegram.send_message(chat_id, "Ingen forfaldne ubetalte fakturaer. 👍")
        return
    linjer = [f"- {f['kunde']} – {f['beloeb']} kr – forfald {f['forfald']} ({f['antal_rykkere']} rykker)"
              for f in fakturaer]
    telegram.send_message(chat_id, "🧾 Forfaldne fakturaer:\n" + "\n".join(linjer))


def vis_aftaler(chat_id, telegram_id):
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db.appointments_between(telegram_id, today + "T00:00:00", today + "T23:59:59")
    if not rows:
        telegram.send_message(chat_id, "Ingen aftaler i dag.")
        return
    linjer = [f"- kl. {r['start'][11:16]} {r.get('kunde') or ''} {r.get('opgave') or ''}".rstrip()
              for r in rows]
    telegram.send_message(chat_id, "📅 Dagens aftaler:\n" + "\n".join(linjer))


# ---------- tekst/stemme-kommandoer ----------

def try_command(chat_id, telegram_id, text):
    """Returnerer True hvis teksten (skrevet ELLER talt) var en menu-kommando."""
    t = (text or "").strip().lower()
    if t in ("/menu", "menu"):
        send_main_menu(chat_id)
        return True
    if "nye ordre" in t:
        vis_nye_ordrer(chat_id, telegram_id)
        return True
    if "dagens ordre" in t:
        vis_dagens_ordrer(chat_id, telegram_id)
        return True
    m = re.search(r"sag\s+(\d+)", t)
    if m and ("vis" in t or "detalj" in t):
        nr = m.group(1)
        case = os_api.get_case(nr)
        if case:
            telegram.send_buttons(chat_id, _kort(case), _ordre_knapper(nr))
        else:
            telegram.send_message(chat_id, f"Fandt ingen sag {nr}.")
        return True
    return False


# ---------- callback-routing ----------

def handle_callback(cq):
    data = cq.get("data", "")
    msg = cq.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    from_id = str(cq.get("from", {}).get("id"))
    telegram.answer_callback(cq.get("id"))

    if data == "nye":
        vis_nye_ordrer(chat_id, from_id)
    elif data == "dagens":
        vis_dagens_ordrer(chat_id, from_id)
    elif data == "fakt":
        vis_forfaldne(chat_id)
    elif data == "aft":
        vis_aftaler(chat_id, from_id)
    elif data.startswith("det:"):
        vis_detaljer(chat_id, message_id, data.split(":", 1)[1])
    elif data.startswith("skjul:"):
        skjul_detaljer(chat_id, message_id, data.split(":", 1)[1])
    elif data.startswith("tildel:"):
        vis_tildel(chat_id, message_id, data.split(":", 1)[1])
    elif data.startswith("sat:"):
        _, case_number, tech_id = data.split(":")
        saet_ansvarlig(chat_id, message_id, case_number, tech_id)

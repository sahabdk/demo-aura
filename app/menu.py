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


def _er_tildelt(case):
    """True hvis sagen allerede har en ansvarlig medarbejder."""
    tech = case.get("main_technician")
    return bool(tech) and str(tech) not in ("0", "None", "")


def _kort(case):
    nr = case.get("case_number")
    kunde = _kundenavn(case.get("customer_number")) or "ukendt"
    desc = (case.get("description") or "")[:50]
    if _er_tildelt(case):
        # Grønt flueben + hvem den er tildelt = overblik for mesteren over hvad der er bearbejdet
        navn = os_api.user_name(case.get("main_technician")) or "tildelt"
        return f"✅ Sag {nr} · {kunde} · {desc} · → {navn}".replace(" ·  · ", " · ").rstrip(" ·")
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

PER_SIDE = 10


def _send_batch(chat_id, cases, offset, header_base, naeste_cb, slut_besked, slut_knapper, pin=False):
    """Vis 10 ordrer ad gangen med knapper. Returnerer True hvis det var sidste side."""
    total = len(cases)
    batch = cases[offset:offset + PER_SIDE]
    omfang = f" ({offset + 1}-{offset + len(batch)})" if total > PER_SIDE else ""
    mid = telegram.send_and_get_id(chat_id, f"{header_base}: {total}{omfang}")
    if pin and mid:
        telegram.pin_message(chat_id, mid)
    for case in batch:
        telegram.send_buttons(chat_id, _kort(case), _ordre_knapper(case.get("case_number")))
    if offset + PER_SIDE < total:
        rest = total - offset - PER_SIDE
        telegram.send_buttons(chat_id, f"↓ {rest} ordrer mere", [[("📋 Vis næste 10", naeste_cb)]])
        return False
    if slut_besked:
        telegram.send_buttons(chat_id, slut_besked, slut_knapper)
    return True


def vis_nye_ordrer(chat_id, telegram_id, offset=0, since=None, pin=True):
    # Strengt kun i dag + kun siden sidst (ingen gentagelse, intet fra tidligere dage)
    if since is None:
        start_idag = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        since = max(db.get_last_seen_order(telegram_id), start_idag)
    cases = os_api.new_cases(since)
    if not cases:
        telegram.send_message(chat_id, "Ingen nye ordrer siden sidst i dag. 👍")
        return
    _send_batch(
        chat_id, cases, offset, "🆕 Nye ordrer siden sidste tjek",
        f"nyeside:{offset + PER_SIDE}:{since}",
        "Resten af dagens ordrer 👇", [[("📋 Se alle dagens ordrer", "dagens")]],
        pin=pin,
    )
    # Markér som set MED DET SAMME (allerede ved første side), så et nyt
    # "nye ordrer" ikke gentager dem. Resten ligger bag "Vis næste 10"-knappen,
    # som bærer det oprindelige 'since' og derfor stadig kan hente dem frem.
    if offset == 0:
        newest = max((int(c.get("created_at") or 0) for c in cases), default=since)
        db.set_last_seen_order(telegram_id, newest + 1)


def vis_dagens_ordrer(chat_id, telegram_id, offset=0):
    cases = os_api.today_cases()
    if not cases:
        telegram.send_message(chat_id, "Ingen ordrer i dag.")
        return
    _send_batch(chat_id, cases, offset, "📋 Dagens ordrer",
                f"dagside:{offset + PER_SIDE}", "", None, pin=False)


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
    telegram.send_message(chat_id, f"🧾 Forfaldne ubetalte fakturaer: {len(fakturaer)}")
    for f in fakturaer[:25]:
        dage = f.get("dage_forsinket")
        forsink = f" · {dage} dage forsinket" if dage else ""
        rykk = f.get("antal_rykkere") or 0
        rtekst = f" · {rykk} rykker sendt" if rykk else " · ingen rykker sendt"
        tekst = f"{f['kunde']} · {f['beloeb']} kr · forfald {f['forfald']}{forsink}{rtekst}"
        telegram.send_buttons(chat_id, tekst, [[("💌 Send rykker", f"rykker:{f['kundenummer']}")]])
    if len(fakturaer) > 25:
        telegram.send_message(chat_id, f"… og {len(fakturaer) - 25} flere.")


def send_rykker_knap(chat_id, message_id, kundenummer, from_id):
    """Sender næste rykker til kunden når lederen trykker på knappen."""
    from .tools import send_paamindelse_email
    bruger = db.get_user(from_id) or {}
    ctx = {"telegram_id": from_id, "navn": bruger.get("navn", ""), "rolle": "pro"}
    res = send_paamindelse_email({"kundenummer": kundenummer}, ctx)
    svar = res.get("resultat") or res.get("fejl") or "Færdig."
    telegram.edit_message(chat_id, message_id, f"💌 {svar}", [])   # fjern knappen så man ikke dobbelt-sender


def vis_aftaler(chat_id, telegram_id):
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db.appointments_between(telegram_id, today + "T00:00:00", today + "T23:59:59")
    if not rows:
        telegram.send_message(chat_id, "Ingen aftaler i dag.")
        return
    linjer = [f"- kl. {r['start'][11:16]} {r.get('kunde') or ''} {r.get('opgave') or ''}".rstrip()
              for r in rows]
    telegram.send_message(chat_id, "📅 Dagens aftaler:\n" + "\n".join(linjer))


def vis_medarbejdere(chat_id):
    """Lister ordrestyrings medarbejdere med id (til at udfylde SEED_USERS korrekt)."""
    try:
        brugere = os_api.users()
    except Exception as e:
        telegram.send_message(chat_id, f"Kunne ikke hente medarbejdere: {e}")
        return
    if not brugere:
        telegram.send_message(chat_id, "Ingen medarbejdere fundet i ordrestyring.")
        return
    linjer = []
    for u in brugere:
        navn = (u.get("fullName") or f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()
                or u.get("init") or "?")
        linjer.append(f"- {navn} (id: {u.get('id')})")
    telegram.send_message(chat_id, "👷 Medarbejdere i ordrestyring:\n" + "\n".join(linjer)
                          + "\n\nBrug id'et i SEED_USERS: telegram_id:navn:jun:ID")


def vis_timetyper(chat_id):
    """Lister ordrestyrings time-typer (Employee types) med id og om de er personlige."""
    try:
        typer = os_api.employee_types()
    except Exception as e:
        telegram.send_message(chat_id, f"Kunne ikke hente timetyper: {e}")
        return
    if not typer:
        telegram.send_message(chat_id, "Ingen timetyper fundet i ordrestyring.")
        return
    linjer = []
    for t in typer:
        pers = " (personlig)" if t.get("is_personal") else ""
        linjer.append(f"- {t.get('title') or '?'} (id: {t.get('id')}){pers}")
    telegram.send_message(chat_id, "⏱ Timetyper i ordrestyring:\n" + "\n".join(linjer))


def vis_raa_timer(chat_id, sagsnummer=None):
    """DEBUG: viser raa timelinjer fra /hours med de tekniske felter (isaer hour_type),
    saa vi kan se hvilke id'er systemet selv gemmer. Skriver ogsaa alt til server-loggen."""
    import json as _json
    try:
        rows = os_api.hours_raw()
        if not isinstance(rows, list):
            rows = [rows]
        if sagsnummer:
            from . import os_graphql as os_gql
            cid = os_gql._case_internal_id(sagsnummer)
            filtreret = [r for r in rows if str(r.get("case_id")) == str(cid)]
            if filtreret:
                rows = filtreret
        rows = rows[-5:]
    except Exception as e:
        telegram.send_message(chat_id, f"Kunne ikke hente raa timer: {e}")
        return
    print(f"[vis_raa_timer] {_json.dumps(rows, ensure_ascii=False, default=str)[:3500]}", flush=True)
    if not rows:
        telegram.send_message(chat_id, "Ingen timelinjer fundet i /hours.")
        return
    linjer = []
    for r in rows:
        linjer.append(_json.dumps(r, ensure_ascii=False, default=str)[:600])
    telegram.send_message(chat_id, "🔧 Raa timelinjer fra ordrestyring:\n\n" + "\n\n".join(linjer))


def vis_graphql_timer(chat_id):
    """DEBUG: lister timer-relaterede queries/mutationer i ordrestyrings GraphQL-skema."""
    from . import os_graphql as os_gql
    try:
        hits, alle = os_gql.find_hour_fields()
    except Exception as e:
        telegram.send_message(chat_id, f"GraphQL-introspektion fejlede: {e}")
        return
    print(f"[vis_graphql_timer] hits={hits}", flush=True)
    print(f"[vis_graphql_timer] ALLE mutationer: {alle.get('mutations')}", flush=True)
    telegram.send_message(
        chat_id,
        "🔧 GraphQL timer-kandidater:\n"
        f"Mutationer: {', '.join(hits.get('mutations') or []) or 'ingen'}\n"
        f"Queries: {', '.join(hits.get('queries') or []) or 'ingen'}\n"
        f"(alle {len(alle.get('mutations') or [])} mutationer ligger i server-loggen)")


def vis_graphql_soeg(chat_id, ord_):
    """DEBUG: soeg i GraphQL-skemaet efter queries/mutationer der matcher et ord."""
    from . import os_graphql as os_gql
    try:
        hits, _ = os_gql.find_hour_fields(words=(ord_.lower(),))
    except Exception as e:
        telegram.send_message(chat_id, f"GraphQL-soegning fejlede: {e}")
        return
    print(f"[vis_graphql_soeg] {ord_}: {hits}", flush=True)
    telegram.send_message(chat_id, f"\U0001f527 GraphQL-felter der matcher '{ord_}':\n"
                          f"Mutationer: {', '.join(hits.get('mutations') or []) or 'ingen'}\n"
                          f"Queries: {', '.join(hits.get('queries') or []) or 'ingen'}")


def vis_graphql_type(chat_id, navn):
    """DEBUG: vis felterne paa en navngiven GraphQL-type (fx PauseType)."""
    from . import os_graphql as os_gql
    try:
        kind, felter = os_gql.describe_type(navn)
    except Exception as e:
        telegram.send_message(chat_id, f"GraphQL-type-opslag fejlede: {e}")
        return
    print(f"[vis_graphql_type] {navn} ({kind}): {felter}", flush=True)
    if not felter:
        telegram.send_message(chat_id, f"Typen {navn} blev ikke fundet (husk store/smaa bogstaver).")
        return
    linjer = [f"- {k}: {v}" for k, v in felter.items()]
    telegram.send_message(chat_id, (f"\U0001f527 {navn} ({kind}):\n" + "\n".join(linjer))[:3800])


def vis_graphql_createhour(chat_id):
    """DEBUG: viser createHour-mutationens argumenter og input-felter."""
    import json as _json
    from . import os_graphql as os_gql
    try:
        args, detaljer = os_gql.describe_hour_input()
    except Exception as e:
        telegram.send_message(chat_id, f"Introspektion fejlede: {e}")
        return
    print(f"[vis_graphql_createhour] args={args}", flush=True)
    print(f"[vis_graphql_createhour] input={_json.dumps(detaljer, ensure_ascii=False)[:3000]}", flush=True)
    linjer = ["createHour(" + ", ".join(f"{k}: {v}" for k, v in args.items()) + ")"]
    for tn, felter in detaljer.items():
        linjer.append(f"\n{tn}:")
        for fn, ft in felter.items():
            linjer.append(f"- {fn}: {ft}")
    telegram.send_message(chat_id, ("🔧 " + "\n".join(linjer))[:3800])


# ---------- tekst/stemme-kommandoer ----------

def try_command(chat_id, telegram_id, text):
    """Returnerer True hvis teksten (skrevet ELLER talt) var en menu-kommando."""
    t = (text or "").strip().lower()
    if t in ("/menu", "menu"):
        send_main_menu(chat_id)
        return True
    mg = re.search(r"graphql\s+s[o\u00f8]g\s+(\S+)", text or "", re.I)   # DEBUG: "vis graphql s\u00f8g pause"
    if mg:
        vis_graphql_soeg(chat_id, mg.group(1))
        return True
    mg = re.search(r"graphql\s+type\s+(\S+)", text or "", re.I)     # DEBUG: "vis graphql type PauseType"
    if mg:
        vis_graphql_type(chat_id, mg.group(1))
        return True
    if "createhour" in t.replace(" ", ""):   # DEBUG: "vis graphql createhour" - felter i mutationen
        vis_graphql_createhour(chat_id)
        return True
    if "graphql" in t and "timer" in t:   # DEBUG: "vis graphql timer" - findes timer-mutationer?
        vis_graphql_timer(chat_id)
        return True
    if "timer" in t and ("rå" in t or "raa" in t or "raw" in t):   # DEBUG: "vis rå timer [på sag 120]"
        m0 = re.search(r"sag\s+(\d+)", t)
        vis_raa_timer(chat_id, m0.group(1) if m0 else None)
        return True
    # "Nye/dagens ordrer" — robust mod talt/naturligt sprog, men ikke når man vil OPRETTE noget
    skab = any(w in t for w in ("opret", "lav ", "tilføj", "registrer", "ny sag på", "opgave på"))
    emne = any(w in t for w in ("ordre", "ordrer", "sag", "sager", "opgave", "opgaver"))
    if not skab and emne:
        if "nye" in t or "nyt" in t or "kommet" in t:   # fx "er der kommet nye ordrer?"
            vis_nye_ordrer(chat_id, telegram_id)
            return True
        if "dagens" in t or "i dag" in t:               # fx "vis dagens ordrer"
            vis_dagens_ordrer(chat_id, telegram_id)
            return True
    if "refer" in t and any(w in t for w in ("scan", "tjek", "find", "mangl")):
        from . import reference
        telegram.send_message(chat_id, reference.scan_and_links())
        return True
    if "medarbejder" in t and any(w in t for w in ("vis", "id", "liste", "list")):
        vis_medarbejdere(chat_id)
        return True
    if "timetyp" in t and any(w in t for w in ("vis", "list", "hvilke", "se")):
        vis_timetyper(chat_id)
        return True
    if "send" not in t and "forfald" in t and any(w in t for w in ("faktura", "regning", "ubetalt")):
        vis_forfaldne(chat_id)
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
    elif data.startswith("nyeside:"):
        _, offset, since = data.split(":")
        vis_nye_ordrer(chat_id, from_id, offset=int(offset), since=int(since), pin=False)
    elif data.startswith("dagside:"):
        vis_dagens_ordrer(chat_id, from_id, offset=int(data.split(":", 1)[1]))
    elif data.startswith("det:"):
        vis_detaljer(chat_id, message_id, data.split(":", 1)[1])
    elif data.startswith("skjul:"):
        skjul_detaljer(chat_id, message_id, data.split(":", 1)[1])
    elif data.startswith("tildel:"):
        vis_tildel(chat_id, message_id, data.split(":", 1)[1])
    elif data.startswith("sat:"):
        _, case_number, tech_id = data.split(":")
        saet_ansvarlig(chat_id, message_id, case_number, tech_id)
    elif data.startswith("rykker:"):
        send_rykker_knap(chat_id, message_id, data.split(":", 1)[1], from_id)

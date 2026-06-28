"""Agentens værktøjer = Python-funktioner + OpenAI-skemaer.

Hvert værktøj angiver hvilke roller der må bruge det. 'pro' = leder, 'jun' = medarbejder.
Funktionerne kaldes med (args: dict, ctx: dict) hvor ctx har 'telegram_id', 'navn', 'rolle'.
"""
import difflib
from datetime import datetime, timedelta
from . import ordrestyring as os_api
from . import db


def _norm(s):
    return (s or "").strip().lower()


def fuzzy_find_customers(query, limit=8):
    """Fleksibel kundesøgning over hele kundelisten (case-insensitiv, delvis, ~match).
    Søger i både navn og adresse, så Aura kan foreslå selv ved upræcis stavning."""
    q = _norm(query)
    if not q:
        return []
    tokens = [t for t in q.split() if len(t) >= 2]
    scored = []
    for d in os_api.all_debtors():
        name = _norm(d.get("customer_name"))
        addr = _norm(d.get("customer_address"))
        city = _norm(d.get("customer_city"))
        hay = f"{name} {addr} {city}"
        if q in name or q in addr or q in city:
            score = 1.0
        elif tokens and all(t in hay for t in tokens):
            score = 0.9          # alle søgeord findes (fx fornavn + by)
        else:
            # Sammenlign både hele teksten og hvert ord med navnet (bedste match tæller)
            ratios = [difflib.SequenceMatcher(None, q, name).ratio()]
            ratios += [difflib.SequenceMatcher(None, t, name).ratio() for t in tokens]
            score = max(ratios)
        scored.append((score, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    # 0.72-tærskel: ægte tastefejl (fx 'kristian'~'christian') fanges, men ikke-relaterede navne ryger fra
    return [d for s, d in scored if s >= 0.72][:limit]


# ---------- værktøjs-implementeringer ----------

def soeg_kunde(args, ctx):
    query = args.get("soegetekst") or args.get("navn") or args.get("adresse") or ""
    rows = fuzzy_find_customers(query)
    if not rows:
        return {"resultat": "ingen kunder fundet", "forslag": []}
    return {"kunder": [
        {"customer_number": r.get("customer_number"), "navn": r.get("customer_name"),
         "adresse": r.get("customer_address"), "postnr": r.get("customer_postalcode"),
         "by": r.get("customer_city")}
        for r in rows
    ]}


def soeg_sager(args, ctx):
    # ordrestyring kan ikke filtrere /cases på customer_number -> filtrér klient-side
    nr = str(args["customer_number"])
    sager = [c for c in os_api.get_cases() if str(c.get("customer_number")) == nr]
    return {"sager": [
        {"sagsnummer": c.get("case_number"),
         "beskrivelse": (c.get("description") or "")[:500],
         "oprettet": c.get("created_at")}
        for c in sager
    ]}


def skriv_bemaerkning(args, ctx):
    dato = datetime.now().strftime("%d-%m-%Y")
    os_api.add_remark(args["sagsnummer"], args["bemaerkning"], dato)
    return {"resultat": f"Bemærkning lagt på sag {args['sagsnummer']}"}


def opret_kunde(args, ctx):
    res = os_api.create_debtor(
        navn=args["navn"], adresse=args["adresse"], postnr=args["postnr"], by=args["by"],
        telefon=args.get("telefon", ""), email=args.get("email", ""),
        mobil=args.get("mobil", ""), attention=args.get("attention", ""), cvr=args.get("cvr", ""),
    )
    return {"resultat": "kunde oprettet", "kundenummer": res.get("customer_number")}


def opdater_kunde(args, ctx):
    res = os_api.update_debtor(args["kundenummer"], **{
        k: v for k, v in args.items() if k != "kundenummer"
    })
    return {"resultat": "kunde opdateret", "kundenummer": args["kundenummer"]}


def _set_kontakt_levering(sagsnummer, customer_number, args):
    """Sæt kontaktperson (sagens fri-tekst contact-felt) og/eller leveringsadresse
    (find-eller-opret en leveringsadresse hos kunden og sæt dens id på sagen)."""
    out = {}
    if not sagsnummer:
        return out
    # Begge dele er IKKE-fatale: en fejl her må aldrig vælte selve sag-oprettelsen.
    if args.get("kontaktperson"):
        try:
            kn = customer_number or (os_api.get_case(sagsnummer) or {}).get("customer_number")
            if not kn:
                out["kontaktperson_fejl"] = "kunne ikke finde kundenummer på sagen"
            else:
                info = os_api.link_kontaktperson(sagsnummer, kn, args["kontaktperson"])
                if info.get("metode") == "kort":
                    out["kontaktperson"] = f"{info['navn']} (sat i Kontaktperson-kortet)"
                else:
                    out["kontaktperson"] = (f"{info['navn']} (lagt i Rekvirenten — findes ikke som "
                                            "fast kontakt på kunden, så Kontaktperson-kortet kan ikke udfyldes via API)")
        except Exception as e:
            out["kontaktperson_fejl"] = str(e)
    if args.get("leveringsadresse"):
        try:
            kn = customer_number or (os_api.get_case(sagsnummer) or {}).get("customer_number")
            if not kn:
                out["leveringsadresse_fejl"] = "kunne ikke finde kundenummer på sagen"
            else:
                info = os_api.link_leveringsadresse(sagsnummer, kn, args["leveringsadresse"])
                out["leveringsadresse"] = ("oprettet og sat" if info.get("oprettet") else "sat") + f": {info.get('adresse')}"
        except Exception as e:
            out["leveringsadresse_fejl"] = str(e)
    return out


def opret_sag(args, ctx):
    res = os_api.create_case(
        customer_number=args["customer_number"],
        beskrivelse=args.get("beskrivelse", ""),
        reference=args.get("reference", ""),
        projektnavn=args.get("projektnavn", ""),
    )
    sag = res.get("case_number")
    # Sæt den der oprettede som ansvarlig — kun hvis de har et ordrestyring-id
    ekstra = {}
    mit_id = (db.get_user(ctx["telegram_id"]) or {}).get("os_user_id")
    if sag and mit_id:
        try:
            os_api.assign_case(sag, mit_id)
            ekstra["ansvarlig"] = ctx.get("navn")
        except Exception:
            pass
    ekstra.update(_set_kontakt_levering(sag, args.get("customer_number"), args))
    return {"resultat": "sag oprettet", "sagsnummer": sag, **ekstra}


def opdater_sag(args, ctx):
    """Tilføj/ret felter på en EKSISTERENDE sag (ingen ny sag oprettes)."""
    beskrivelse = args.get("beskrivelse")
    if args.get("projektnavn"):
        cur = os_api.get_case(args["sagsnummer"]) or {}
        beskrivelse = os_api._med_projekt(beskrivelse or cur.get("description", ""), args["projektnavn"])
    os_api.update_case(
        args["sagsnummer"],
        beskrivelse=beskrivelse,
        reference=args.get("reference"),
    )
    ekstra = _set_kontakt_levering(args["sagsnummer"], None, args)
    return {"resultat": f"Sag {args['sagsnummer']} opdateret", **ekstra}


def afslut_sag(args, ctx):
    # Medarbejdere (jun) må kun færdigmelde sager der er tildelt DEM
    if ctx["rolle"] != "pro":
        case = os_api.get_case(args["sagsnummer"]) or {}
        mit_id = (db.get_user(ctx["telegram_id"]) or {}).get("os_user_id")
        tildelt = str(case.get("main_technician") or "")
        if not mit_id or str(mit_id) != tildelt:
            return {"resultat": "Du kan kun færdigmelde dine egne opgaver — altså dem der er tildelt dig. "
                                "Bed lederen, hvis en anden sag skal lukkes."}
    info = os_api.close_case(args["sagsnummer"], work_done=args.get("kommentar", ""))
    if info.get("status_id"):
        return {"resultat": f"Sag {args['sagsnummer']} er færdigmeldt og lukket (status sat til afsluttet)."}
    return {"resultat": (f"Sag {args['sagsnummer']} er færdigmeldt (arbejde noteret), men jeg kunne ikke "
                         "finde en 'afsluttet'-status at sætte, så status er uændret.")}


def send_paamindelse_email(args, ctx):
    nr = str(args["kundenummer"])
    debtor = os_api.get_debtor(nr)
    email = debtor.get("customer_email")
    navn = debtor.get("customer_name")
    if not email:
        return {"resultat": "kunden har ingen email - kan ikke sende rykker"}

    # Find kundens forfaldne faktura(er) -> nævn beløb og forfald i mailen
    beloeb = forfald = None
    try:
        mine = [r for r in os_api.overdue_unpaid_invoices()
                if str(r.get("customer_number")) == nr]
        if mine:
            total = sum(float(r.get("amount_vat") or 0) for r in mine)
            beloeb = f"{total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            pd = mine[0].get("payment_date")
            if pd:
                forfald = datetime.fromtimestamp(int(pd)).strftime("%d-%m-%Y")
    except Exception:
        pass

    # Beregn næste niveau UDEN at gemme endnu (tæl kun op hvis mailen faktisk sendes)
    level = min(3, db.get_reminder_count(nr) + 1)
    from .email import send_payment_reminder
    try:
        status = send_payment_reminder(email, navn, level, beloeb=beloeb, forfald=forfald)
    except Exception as e:
        return {"fejl": f"rykker kunne ikke sendes: {e}"}

    if status != "sent":
        return {"resultat": (f"TEST-TILSTAND: ingen rigtig mail sendt (SMTP er ikke sat op endnu). "
                             f"Det ville have været {level}. påmindelse til {navn}.")}
    db.set_reminder_count(nr, level)  # tæl først op ved bekræftet afsendelse
    return {"resultat": f"{level}. påmindelse sendt til {navn} ({email})"}


def forfaldne_fakturaer(args, ctx):
    rows = os_api.overdue_unpaid_invoices()
    out = []
    for r in rows:
        pd = r.get("payment_date")
        try:
            forfald = datetime.fromtimestamp(int(pd)).strftime("%d-%m-%Y") if pd else ""
        except (ValueError, TypeError, OSError):
            forfald = str(pd or "")
        knr = r.get("customer_number")
        out.append({"kunde": r.get("cust_name"), "kundenummer": knr,
                    "sag": r.get("case_number"), "beloeb": r.get("amount_vat"),
                    "forfald": forfald, "antal_rykkere": db.get_reminder_count(knr)})
    return {"antal": len(out), "fakturaer": out}


def saet_rykker_niveau(args, ctx):
    """Lederen synkroniserer tælleren med manuelt afsendte rykkere."""
    db.set_reminder_count(args["kundenummer"], int(args.get("antal", 0)))
    return {"resultat": f"Kunde {args['kundenummer']} er nu registreret som rykket {args['antal']} gang(e)"}


def husk_aftale(args, ctx):
    db.add_appointment(ctx["telegram_id"], args.get("kunde"), args.get("opgave"), args["start"])
    return {"resultat": f"Husket: {args.get('opgave')} ({args['start']})"}


def se_aftaler(args, ctx):
    fra = args["fra"] + "T00:00:00"
    til = args["til"] + "T23:59:59"
    rows = db.appointments_between(ctx["telegram_id"], fra, til)
    return {"aftaler": [{"start": r["start"], "kunde": r["kunde"], "opgave": r["opgave"]} for r in rows]}


# ---------- registry: skema + funktion + tilladte roller ----------

TOOLS = [
    {
        "func": soeg_kunde, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "soeg_kunde",
            "description": "Søg en kunde fleksibelt via navn ELLER adresse (delvis/upræcis er ok — den foreslår). "
                           "Send ét felt 'soegetekst' med det brugeren sagde, fx 'christian' eller 'Ribevej 25'. "
                           "Returnerer en liste af mulige kunder med customer_number, navn, adresse, postnr, by.",
            "parameters": {"type": "object", "properties": {
                "soegetekst": {"type": "string"}}, "required": ["soegetekst"]},
        }},
    },
    {
        "func": soeg_sager, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "soeg_sager",
            "description": "Find en kundes sager via customer_number. Returnerer sagsnummer, beskrivelse, dato.",
            "parameters": {"type": "object", "properties": {
                "customer_number": {"type": "string"}}, "required": ["customer_number"]},
        }},
    },
    {
        "func": skriv_bemaerkning, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "skriv_bemaerkning",
            "description": "Skriv en bemærkning på en sag (historik bevares). sagsnummer = kun tallet. "
                           "bemaerkning = kun selve noten, uden kundenavn/adresse.",
            "parameters": {"type": "object", "properties": {
                "sagsnummer": {"type": "string"}, "bemaerkning": {"type": "string"}},
                "required": ["sagsnummer", "bemaerkning"]},
        }},
    },
    {
        "func": opret_kunde, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "opret_kunde",
            "description": "Opret en NY kunde. Påkrævet: navn, adresse, postnr, by. Valgfrit (kun hvis "
                           "brugeren nævner dem): telefon, email, mobil, attention, cvr. Returnerer kundenummer.",
            "parameters": {"type": "object", "properties": {
                "navn": {"type": "string"}, "adresse": {"type": "string"},
                "postnr": {"type": "string"}, "by": {"type": "string"},
                "telefon": {"type": "string"}, "email": {"type": "string"},
                "mobil": {"type": "string"}, "attention": {"type": "string"}, "cvr": {"type": "string"}},
                "required": ["navn", "adresse", "postnr", "by"]},
        }},
    },
    {
        "func": opdater_kunde, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "opdater_kunde",
            "description": "Opdater en EKSISTERENDE kunde (fx tilføj cvr, ret telefon/email/mobil). "
                           "kundenummer påkrævet; medtag kun felter der skal ændres.",
            "parameters": {"type": "object", "properties": {
                "kundenummer": {"type": "string"}, "cvr": {"type": "string"},
                "telefon": {"type": "string"}, "email": {"type": "string"}, "mobil": {"type": "string"}},
                "required": ["kundenummer"]},
        }},
    },
    {
        "func": opret_sag, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "opret_sag",
            "description": "Opret en NY sag på en eksisterende kunde. Brug KUN når brugeren tydeligt vil have en ny sag. "
                           "Valgfrit: projektnavn, reference, kontaktperson, leveringsadresse. Returnerer sagsnummer.",
            "parameters": {"type": "object", "properties": {
                "customer_number": {"type": "string"}, "beskrivelse": {"type": "string"},
                "projektnavn": {"type": "string"}, "reference": {"type": "string"},
                "kontaktperson": {"type": "string"}, "leveringsadresse": {"type": "string"}},
                "required": ["customer_number"]},
        }},
    },
    {
        "func": opdater_sag, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "opdater_sag",
            "description": "Tilføj/ret felter på en EKSISTERENDE sag (opretter ALDRIG en ny). Brug når brugeren vil "
                           "tilføje noget til en sag der findes, fx ret beskrivelse, sæt projektnavn, reference, kontaktperson eller leveringsadresse.",
            "parameters": {"type": "object", "properties": {
                "sagsnummer": {"type": "string"}, "beskrivelse": {"type": "string"},
                "projektnavn": {"type": "string"}, "reference": {"type": "string"},
                "kontaktperson": {"type": "string"}, "leveringsadresse": {"type": "string"}},
                "required": ["sagsnummer"]},
        }},
    },
    {
        "func": afslut_sag, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "afslut_sag",
            "description": "Færdigmeld en sag og læg en afsluttende kommentar i 'Færdiggjort arbejde'.",
            "parameters": {"type": "object", "properties": {
                "sagsnummer": {"type": "string"}, "kommentar": {"type": "string"}},
                "required": ["sagsnummer"]},
        }},
    },
    {
        "func": send_paamindelse_email, "roles": {"pro"},   # KUN leder
        "schema": {"type": "function", "function": {
            "name": "send_paamindelse_email",
            "description": "Send en eskalerende betalingspåmindelse til en kunde (systemet vælger 1./2./3. niveau). "
                           "Brug kun efter bekræftelse.",
            "parameters": {"type": "object", "properties": {
                "kundenummer": {"type": "string"}}, "required": ["kundenummer"]},
        }},
    },
    {
        "func": forfaldne_fakturaer, "roles": {"pro"},   # KUN leder
        "schema": {"type": "function", "function": {
            "name": "forfaldne_fakturaer",
            "description": "Henter forfaldne, ubetalte fakturaer (kun leder). Returnerer liste med kunde, kundenummer, sag, beløb, forfaldsdato og antal_rykkere (hvor mange gange kunden er rykket).",
            "parameters": {"type": "object", "properties": {}},
        }},
    },
    {
        "func": saet_rykker_niveau, "roles": {"pro"},   # KUN leder
        "schema": {"type": "function", "function": {
            "name": "saet_rykker_niveau",
            "description": "Synkronisér rykker-tælleren for en kunde med manuelt afsendte rykkere. "
                           "Brug fx når lederen siger 'Christian er allerede rykket 2 gange manuelt'.",
            "parameters": {"type": "object", "properties": {
                "kundenummer": {"type": "string"}, "antal": {"type": "integer"}},
                "required": ["kundenummer", "antal"]},
        }},
    },
    {
        "func": husk_aftale, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "husk_aftale",
            "description": "Gem en aftale. start = ISO ÅÅÅÅ-MM-DDTHH:mm:ss (brug T08:00:00 hvis intet klokkeslæt).",
            "parameters": {"type": "object", "properties": {
                "kunde": {"type": "string"}, "opgave": {"type": "string"}, "start": {"type": "string"}},
                "required": ["opgave", "start"]},
        }},
    },
    {
        "func": se_aftaler, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "se_aftaler",
            "description": "Hent aftaler i et datointerval (fra/til som ÅÅÅÅ-MM-DD).",
            "parameters": {"type": "object", "properties": {
                "fra": {"type": "string"}, "til": {"type": "string"}}, "required": ["fra", "til"]},
        }},
    },
]

BY_NAME = {t["schema"]["function"]["name"]: t for t in TOOLS}


def schemas_for_role(rolle: str):
    return [t["schema"] for t in TOOLS if rolle in t["roles"]]


def call_tool(name: str, args: dict, ctx: dict):
    tool = BY_NAME.get(name)
    if not tool:
        return {"fejl": f"ukendt værktøj {name}"}
    if ctx["rolle"] not in tool["roles"]:
        return {"fejl": "afvist: kun lederen kan bruge denne funktion"}
    try:
        return tool["func"](args, ctx)
    except Exception as e:  # ægte fejl -> agenten fortæller ærligt at det fejlede
        return {"fejl": str(e)}
# slut

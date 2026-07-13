"""Agentens værktøjer = Python-funktioner + OpenAI-skemaer.

Hvert værktøj angiver hvilke roller der må bruge det. 'pro' = leder, 'jun' = medarbejder.
Funktionerne kaldes med (args: dict, ctx: dict) hvor ctx har 'telegram_id', 'navn', 'rolle'.
"""
import difflib
from datetime import datetime, timedelta
from . import ordrestyring as os_api
from . import os_graphql as os_gql
from . import db
from .config import TZ, now_local


def _norm(s):
    return (s or "").strip().lower()


def fuzzy_find_customers(query, limit=8, with_scores=False):
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
        matched = sum(1 for t in tokens if t in hay)
        if q in name or q in addr or q in city:
            score = 1.0
        elif tokens and matched == len(tokens):
            score = 0.97         # ALLE søgeord findes (fx navn + vej) -> stærkeste match
        elif tokens and matched:
            # Delvist: gav brugeren flere ord (fx navn + vej) men kun nogle passer, så
            # er det et svagere match end en fuld-træffer. Skalér efter hvor stor en andel
            # der matcher, så navn-kun-match ikke overhaler et navn+vej-match.
            frac = matched / len(tokens)
            fuzzy = max([difflib.SequenceMatcher(None, t, name).ratio() for t in tokens] + [0])
            score = min(0.88, 0.55 + 0.33 * frac, max(fuzzy, 0.5 * frac + 0.4))
        else:
            ratios = [difflib.SequenceMatcher(None, q, name).ratio()]
            ratios += [difflib.SequenceMatcher(None, t, name).ratio() for t in tokens]
            score = max(ratios)
        scored.append((score, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    # 0.72-tærskel: ægte tastefejl (fx 'kristian'~'christian') fanges, men ikke-relaterede navne ryger fra
    hits = [(s, d) for s, d in scored if s >= 0.72][:limit]
    return hits if with_scores else [d for s, d in hits]


# ---------- værktøjs-implementeringer ----------

def soeg_kunde(args, ctx):
    query = args.get("soegetekst") or args.get("navn") or args.get("adresse") or ""
    scored = fuzzy_find_customers(query, with_scores=True)
    if not scored:
        return {"resultat": "ingen kunder fundet", "forslag": []}
    kunder = [
        {"customer_number": r.get("customer_number"), "navn": r.get("customer_name"),
         "adresse": r.get("customer_address"), "postnr": r.get("customer_postalcode"),
         "by": r.get("customer_city")}
        for s, r in scored
    ]
    # Klart bedste match: enten kun én, eller topscoren ligger tydeligt over næste.
    # Så kan Aura vælge direkte uden at spørge (fx bruger gav navn + vej der kun passer på én).
    top = scored[0][0]
    naest = scored[1][0] if len(scored) > 1 else 0
    entydig = len(scored) == 1 or (top >= 0.9 and top - naest >= 0.1)
    return {"kunder": kunder, "entydigt_match": entydig,
            "bedste": kunder[0] if entydig else None}


def soeg_sager(args, ctx):
    # ordrestyring kan ikke filtrere /cases på customer_number -> hent (pagineret) og filtrér klient-side
    nr = str(args["customer_number"])
    sager = [c for c in os_api.cases_paged() if str(c.get("customer_number")) == nr]
    sager.sort(key=lambda c: int(c.get("created_at") or 0), reverse=True)
    return {"sager": [
        {"sagsnummer": c.get("case_number"),
         "beskrivelse": (c.get("description") or "")[:500] or "(ingen beskrivelse)",
         "aaben": not os_api.is_closed(c),
         "oprettet": c.get("created_at")}
        for c in sager
    ]}


def mine_sager(args, ctx):
    """Sager tildelt den bruger der spørger (via deres ordrestyring-id)."""
    mit_id = (db.get_user(ctx["telegram_id"]) or {}).get("os_user_id")
    if not mit_id:
        return {"resultat": "Din bruger er ikke koblet til en medarbejder i ordrestyring, "
                            "så jeg kan ikke se hvilke sager der er tildelt dig. Bed lederen om at koble dig."}
    mine = [c for c in os_api.cases_paged() if str(c.get("main_technician") or "") == str(mit_id)]
    if not args.get("inkluder_lukkede"):
        mine = [c for c in mine if not os_api.is_closed(c)]
    mine.sort(key=lambda c: int(c.get("created_at") or 0), reverse=True)
    return {"antal": len(mine), "sager": [
        {"sagsnummer": c.get("case_number"),
         "beskrivelse": (c.get("description") or "")[:120] or "(ingen beskrivelse)"}
        for c in mine[:30]
    ]}


def _ejer_eller_afvis(sagsnummer, ctx):
    """Returnerer en afvisnings-dict hvis en jun rører en sag der IKKE er tildelt dem.
    Lederen (pro) må alt -> None. Bruges af kommentar/redigér/færdigmeld."""
    if ctx.get("rolle") == "pro":
        return None
    case = os_api.get_case(sagsnummer) or {}
    mit_id = (db.get_user(ctx["telegram_id"]) or {}).get("os_user_id")
    tildelt = str(case.get("main_technician") or "")
    if not mit_id or str(mit_id) != tildelt:
        return {"resultat": "Du kan kun kommentere og redigere dine egne opgaver — altså dem der er "
                            "tildelt dig. Bed lederen, hvis en anden sag skal ændres."}
    return None


def skriv_bemaerkning(args, ctx):
    afvist = _ejer_eller_afvis(args["sagsnummer"], ctx)
    if afvist:
        return afvist
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
            info = os_gql.set_case_contact_person(sagsnummer, kn, args["kontaktperson"])
            handling = "oprettet og sat" if info.get("oprettet") else "sat"
            out["kontaktperson"] = f"{info['navn']} ({handling} i Kontaktperson-kortet)"
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
    afvist = _ejer_eller_afvis(args["sagsnummer"], ctx)
    if afvist:
        return afvist
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
    if _ejer_eller_afvis(args["sagsnummer"], ctx):
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

    # Find kundens forfaldne faktura(er) -> beløb, forfald, dage forsinket, fakturanr i mailen
    beloeb = forfald = fakturanr = None
    dage_forsinket = None
    try:
        mine = [r for r in os_api.overdue_unpaid_invoices()
                if str(r.get("customer_number")) == nr]
        if mine:
            total = sum(float(r.get("amount_vat") or 0) for r in mine)
            beloeb = f"{total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            pds = [int(r.get("payment_date")) for r in mine if r.get("payment_date")]
            if pds:
                aeldste = min(pds)   # mest forfaldne
                forfald = datetime.fromtimestamp(aeldste).strftime("%d-%m-%Y")
                dage_forsinket = max(0, int((datetime.now().timestamp() - aeldste) / 86400))
            fakturanr = ", ".join(str(r.get("invoice_number")) for r in mine if r.get("invoice_number"))
    except Exception:
        pass

    # Niveau: lederen kan bede om et bestemt (1/2/3); ellers vælger systemet næste automatisk.
    if args.get("niveau"):
        level = max(1, min(3, int(args["niveau"])))
    else:
        level = min(3, db.get_reminder_count(nr) + 1)
    from .email import send_payment_reminder
    try:
        status = send_payment_reminder(email, navn, level, beloeb=beloeb, forfald=forfald,
                                       dage_forsinket=dage_forsinket, fakturanr=fakturanr or None)
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
        forfald, dage = "", None
        try:
            if pd:
                forfald = datetime.fromtimestamp(int(pd)).strftime("%d-%m-%Y")
                dage = max(0, int((datetime.now().timestamp() - int(pd)) / 86400))
        except (ValueError, TypeError, OSError):
            forfald = str(pd or "")
        knr = r.get("customer_number")
        out.append({"kunde": r.get("cust_name"), "kundenummer": knr,
                    "sag": r.get("case_number"), "beloeb": r.get("amount_vat"),
                    "forfald": forfald, "dage_forsinket": dage,
                    "antal_rykkere": db.get_reminder_count(knr)})
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


def _pris(p):
    """Vis pris pænt hvis muligt."""
    v = p.get("price_kr")
    if v in (None, ""):
        return None
    try:
        return f"{float(v):.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " kr"
    except (ValueError, TypeError):
        return str(v)


def soeg_vare(args, ctx):
    """Søg i vare-kataloget. Returnerer op til 5 forslag + hvor mange flere der er."""
    term = (args.get("soegetekst") or "").strip()
    if not term:
        return {"resultat": "skriv hvad varen hedder"}
    try:
        varer, flere = os_gql.search_products(term, limit=5)
    except Exception as e:
        return {"fejl": f"varesøgning fejlede: {e}"}
    if not varer:
        return {"resultat": f"ingen varer fundet for '{term}'", "varer": []}
    return {"varer": [
        {"vare_id": p.get("id"), "varenummer": p.get("number"),
         "beskrivelse": p.get("description"), "pris": _pris(p)}
        for p in varer
    ], "flere": flere}


def tilfoej_vare(args, ctx):
    """Læg en vare på en sag (kræver sagsnummer + vare_id fra soeg_vare)."""
    afvist = _ejer_eller_afvis(args["sagsnummer"], ctx)
    if afvist:
        return afvist
    try:
        os_gql.add_case_material(
            args["sagsnummer"],
            identifier=args.get("vare_id"),
            quantity=args.get("antal", 1),
            product_number=args.get("varenummer"),
            description=args.get("beskrivelse"),
        )
    except Exception as e:
        return {"fejl": f"kunne ikke lægge varen på sagen: {e}"}
    antal = args.get("antal", 1)
    navn = args.get("beskrivelse") or args.get("varenummer") or "varen"
    return {"resultat": f"Lagt på sag {args['sagsnummer']}: {navn} × {antal}"}


def _unix_ts(dato, hhmm):
    """Lav unix-sekunder ud fra dato 'YYYY-MM-DD' + klokkeslaet 'HH:MM' i dansk tid."""
    dt = datetime.strptime(f"{dato} {hhmm}", "%Y-%m-%d %H:%M")
    try:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo(TZ))
    except Exception:
        pass
    return int(dt.timestamp())


def _find_user_id(navn):
    """Slaa en medarbejders ordrestyring-id op ud fra navn eller initialer."""
    nl = (navn or "").strip().lower()
    if not nl:
        return None
    for u in os_api.users():
        full = (u.get("fullName") or f"{u.get('first_name','') or ''} {u.get('last_name','') or ''}").strip().lower()
        if nl == full or (full and nl in full) or (u.get("init") or "").lower() == nl:
            return u.get("id")
    return None


def registrer_timer(args, ctx):
    """Registrer arbejdstimer paa en sag (fra/til-klokkeslaet, type, beskrivelse, medarbejder)."""
    sag = args["sagsnummer"]
    afvist = _ejer_eller_afvis(sag, ctx)
    if afvist:
        return afvist
    fra = (args.get("fra") or "").strip()
    til = (args.get("til") or "").strip()
    if not fra or not til:
        return {"resultat": "jeg mangler baade fra- og til-klokkeslaet (fx 08:00 til 15:30)"}
    dato = (args.get("dato") or "").strip() or now_local().strftime("%Y-%m-%d")
    try:
        start = _unix_ts(dato, fra)
        stop = _unix_ts(dato, til)
    except ValueError:
        return {"fejl": "kunne ikke forstaa dato eller klokkeslaet"}
    if stop <= start:
        return {"fejl": "sluttidspunktet skal vaere efter starttidspunktet"}
    # Medarbejder: eksplicit navn > koblet ordrestyring-id > match paa den der spoerger sit eget navn
    emp_id = None
    if args.get("medarbejder"):
        emp_id = _find_user_id(args["medarbejder"])
    if not emp_id:
        emp_id = (db.get_user(ctx["telegram_id"]) or {}).get("os_user_id")
    if not emp_id:
        emp_id = _find_user_id(ctx.get("navn"))
    if not emp_id:
        return {"resultat": "jeg kunne ikke finde din medarbejder i ordrestyring. Sig hvilket "
                            "medarbejdernavn timerne skal paa (fx paa Mads Hansen)."}
    # Beskrivelse + pause/tillaeg (API'et har ikke egne felter -> noteres i teksten)
    remark = (args.get("beskrivelse") or "").strip()
    ekstra = []
    if args.get("pause_min"):
        try:
            ekstra.append(f"pause {int(args['pause_min'])} min")
        except (ValueError, TypeError):
            pass
    if args.get("tillaeg"):
        ekstra.append(f"tillaeg: {args['tillaeg']}")
    if ekstra:
        remark = (remark + " (" + ", ".join(ekstra) + ")").strip()
    cid = os_gql._case_internal_id(sag)
    if not cid:
        return {"fejl": f"kunne ikke finde sag {sag}"}
    # Time-typer: medarbejderen har maaske kun lov til nogle. Proev den oenskede/standard
    # foerst; afvises den paa rettigheder, proev de oevrige typer indtil en gaar igennem.
    typer = os_api.employee_types()
    kandidater = []
    valgt = os_api.find_hour_type(args.get("type"))
    if valgt:
        kandidater.append(valgt)
    if not args.get("type"):   # ingen bestemt type oensket -> fald tilbage til de andre
        for t in typer:
            tid = t.get("id")
            if tid and tid not in kandidater:
                kandidater.append(tid)
    if not kandidater:
        return {"fejl": "kunne ikke finde en time-type i ordrestyring"}
    brugt, sidste_fejl = None, None
    for ht in kandidater:
        try:
            os_api.register_hours(case_id=cid, emp_id=emp_id, start_time=start, stop_time=stop,
                                  hour_type=ht, remark=remark)
            brugt = ht
            break
        except Exception as e:
            sidste_fejl = str(e)
            lav = sidste_fejl.lower()
            if "timetype" not in lav and "lov til" not in lav:
                break   # anden slags fejl -> stop, det hjaelper ikke at proeve flere typer
    if brugt is None:
        return {"fejl": f"kunne ikke registrere timer: {sidste_fejl}"}
    brutto = round((stop - start) / 3600, 2)
    type_navn = next((t.get("title") for t in typer if t.get("id") == brugt), "")
    svar = f"Registreret {brutto} timer paa sag {sag} ({fra}-{til} den {dato})"
    if type_navn:
        svar += f", type: {type_navn}"
    return {"resultat": svar}


# ---------- registry: skema + funktion + tilladte roller ----------

TOOLS = [
    {
        "func": registrer_timer, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "registrer_timer",
            "description": "Registrer arbejdstimer paa en sag (Timer-fanen): hvor laenge arbejdet tog. "
                           "Angiv sagsnummer + fra og til som klokkeslaet (HH:MM). Valgfrit: dato (YYYY-MM-DD, "
                           "default i dag), type (time-type som 'normal'/'overtid'), beskrivelse, medarbejder "
                           "(navn - default den der spoerger), pause_min (pause i minutter) og tillaeg. "
                           "Pause og tillaeg noteres i beskrivelsen.",
            "parameters": {"type": "object", "properties": {
                "sagsnummer": {"type": "string"}, "fra": {"type": "string"}, "til": {"type": "string"},
                "dato": {"type": "string"}, "type": {"type": "string"},
                "beskrivelse": {"type": "string"}, "medarbejder": {"type": "string"},
                "pause_min": {"type": "integer"}, "tillaeg": {"type": "string"}},
                "required": ["sagsnummer", "fra", "til"]},
        }},
    },
    {
        "func": soeg_kunde, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "soeg_kunde",
            "description": "Søg en kunde fleksibelt via navn ELLER adresse (delvis/upræcis er ok — den foreslår). "
                           "Send ét felt 'soegetekst' med ALT brugeren sagde, fx 'christian' eller 'christian ribevej 25'. "
                           "Returnerer 'kunder' (customer_number, navn, adresse, postnr, by), 'entydigt_match' (true "
                           "hvis der klart kun er én rigtig) og 'bedste' (den kunde du så kan bruge direkte).",
            "parameters": {"type": "object", "properties": {
                "soegetekst": {"type": "string"}}, "required": ["soegetekst"]},
        }},
    },
    {
        "func": soeg_sager, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "soeg_sager",
            "description": "Find en kundes sager via customer_number. Returnerer sagsnummer, beskrivelse, om sagen er åben, dato.",
            "parameters": {"type": "object", "properties": {
                "customer_number": {"type": "string"}}, "required": ["customer_number"]},
        }},
    },
    {
        "func": soeg_vare, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "soeg_vare",
            "description": "Søg i vare-/materiale-kataloget (fx 'muffe', 'muffe 28mm'). Returnerer op til 5 "
                           "varer (vare_id, varenummer, beskrivelse, pris) + 'flere' = hvor mange flere der er. "
                           "Præciserer brugeren søgningen, kommer der færre.",
            "parameters": {"type": "object", "properties": {
                "soegetekst": {"type": "string"}}, "required": ["soegetekst"]},
        }},
    },
    {
        "func": tilfoej_vare, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "tilfoej_vare",
            "description": "Læg en vare/materiale på en sag. Brug vare_id fra soeg_vare. antal = mængde (default 1). "
                           "Medtag gerne varenummer og beskrivelse fra den valgte vare.",
            "parameters": {"type": "object", "properties": {
                "sagsnummer": {"type": "string"}, "vare_id": {"type": "string"},
                "antal": {"type": "number"}, "varenummer": {"type": "string"},
                "beskrivelse": {"type": "string"}},
                "required": ["sagsnummer", "vare_id"]},
        }},
    },
    {
        "func": mine_sager, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "mine_sager",
            "description": "Sager der er tildelt den bruger der spørger (fx 'hvor mange sager har jeg', "
                           "'mine sager', 'hvad ligger der til mig'). Som standard kun åbne sager. "
                           "Returnerer antal og en liste med sagsnummer + beskrivelse.",
            "parameters": {"type": "object", "properties": {
                "inkluder_lukkede": {"type": "boolean"}}},
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
            "description": "Send en betalingspåmindelse (rykker) til en kunde. Angiv 'niveau' (1, 2 eller 3) "
                           "hvis lederen beder om et bestemt niveau ('1. rykker', 'sidste rykker'=3). "
                           "Uden niveau vælger systemet automatisk næste. En rykker hører til KUNDEN, ikke en sag.",
            "parameters": {"type": "object", "properties": {
                "kundenummer": {"type": "string"},
                "niveau": {"type": "integer", "description": "1, 2 eller 3 — valgfrit"}},
                "required": ["kundenummer"]},
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

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
    def _er_med(c):
        for k in ("users", "workers", "employees"):
            v = c.get(k)
            if isinstance(v, list):
                for u in v:
                    uid = u.get("id") if isinstance(u, dict) else u
                    if str(uid) == str(mit_id):
                        return True
        return False
    mine = [c for c in os_api.cases_paged()
            if str(c.get("main_technician") or "") == str(mit_id) or _er_med(c)]
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
    if mit_id and str(mit_id) == tildelt:
        return None
    if mit_id:   # ogsaa ok hvis medarbejderen staar paa sagens Medarbejdere-liste
        try:
            _, uids = os_gql.case_user_ids(sagsnummer)
            if int(mit_id) in [int(u) for u in uids]:
                return None
        except Exception:
            pass
    return {"resultat": "Du kan kun kommentere og redigere dine egne opgaver — altså dem der er "
                        "tildelt dig. Bed lederen, hvis en anden sag skal ændres."}


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
        _mat = os_gql.add_case_material(
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
    ud = {"resultat": f"Lagt på sag {args['sagsnummer']}: {navn} × {antal}"}
    if (_mat or {}).get("id"):
        ud["_ref"] = {"type": "material", "id": _mat["id"]}
    return ud


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
    """Slaa en medarbejders ordrestyring-id op ud fra navn eller initialer.
    Fejltolerant: 'Demitri' rammer 'Dmitri' (tale-/stavefejl)."""
    nl = (navn or "").strip().lower()
    if not nl:
        return None
    bedste, bedste_score = None, 0.0
    for u in os_api.users():
        full = (u.get("fullName") or f"{u.get('first_name','') or ''} {u.get('last_name','') or ''}").strip().lower()
        if nl == full or (full and nl in full) or (u.get("init") or "").lower() == nl:
            return u.get("id")
        # fuzzy: sammenlign mod hele navnet OG hvert enkelt navn-led
        for kandidat in [full] + full.split():
            r = difflib.SequenceMatcher(None, nl, kandidat).ratio()
            if r > bedste_score:
                bedste, bedste_score = u.get("id"), r
    return bedste if bedste_score >= 0.75 else None


def _find_pause(pause_min):
    """Vaelg pause-type + antal ud fra oensket antal minutter (fx 60 -> 2 x Frokost a 30)."""
    try:
        typer = os_gql.pause_types()
    except Exception as e:
        print(f"[_find_pause] kunne ikke hente pause-typer: {str(e)[:200]}", flush=True)
        return None
    pm = int(pause_min)
    if pm <= 0 or not typer:
        print(f"[_find_pause] ingen brugbare pause-typer (pm={pm}, typer={typer})", flush=True)
        return None
    bedst = None
    for pt in typer:
        m = int(pt.get("minutes") or 0)
        if m <= 0 or pt.get("id") is None:
            continue
        if pm % m == 0:   # praecist multiplum -> fx 60 min = 2 x 30-min-pause
            return {"pauseTypeId": int(pt["id"]), "quantity": pm // m,
                    "navn": pt.get("name") or "pause", "minutter": pm}
        if bedst is None or abs(m - pm) < abs(int(bedst.get("minutes") or 0) - pm):
            bedst = pt
    if bedst is None:   # ingen type har varighed sat -> brug den foerste alligevel
        bedst = next((pt for pt in typer if pt.get("id") is not None), None)
    if bedst:
        return {"pauseTypeId": int(bedst["id"]), "quantity": 1,
                "navn": bedst.get("name") or "pause", "minutter": int(bedst.get("minutes") or 0)}
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
    # Beskrivelse + tillaeg (tillaeg noteres i teksten). Pause registreres som
    # RIGTIG pause via GraphQL ud fra firmaets pause-typer (fx Frokost 30 min).
    remark = (args.get("beskrivelse") or "").strip()
    ekstra = []
    pause = None
    if args.get("pause_min"):
        try:
            pause = _find_pause(int(args["pause_min"]))
        except (ValueError, TypeError):
            pause = None
        print(f"[registrer_timer] pause_min={args.get('pause_min')} -> {pause}", flush=True)
        if pause and pause["minutter"] != int(args.get("pause_min") or 0):
            ekstra.append(f"pause oensket {args['pause_min']} min - registreret "
                          f"{pause['quantity']} x {pause['navn']} ({pause['minutter']} min)")
        if not pause:
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
    if not args.get("type"):   # ingen bestemt type -> fald tilbage til ARBEJDS-typer (ikke Syg/Ferie osv.)
        for t in typer:
            tid = t.get("id")
            if tid and tid not in kandidater and not t.get("is_personal"):
                kandidater.append(tid)
    if not kandidater:
        return {"fejl": "kunne ikke finde en time-type i ordrestyring"}
    brugt, fejl_pr_type, oprettet_id = None, [], None
    for ht in kandidater:
        # 1) GraphQL createHour (v2 POST /hours er blokeret paa API-noeglens rettigheder)
        try:
            _ch = os_gql.create_hour(case_id=cid, user_id=emp_id, hour_type_id=ht,
                               start_time=start, stop_time=stop, description=remark or None,
                               pauses=[{"pauseTypeId": pause["pauseTypeId"],
                                        "quantity": pause["quantity"]}] if pause else None)
            brugt, oprettet_id = ht, (_ch or {}).get("id")
            print(f"[registrer_timer] GraphQL createHour OK (hourTypeId {ht}, id {oprettet_id})", flush=True)
            break
        except Exception as eg:
            print(f"[registrer_timer] GraphQL createHour fejlede (hourTypeId {ht}): {str(eg)[:200]}", flush=True)
        # 2) fallback: v2 REST (virker den dag ordrestyring aabner noeglen)
        try:
            _rh = os_api.register_hours(case_id=cid, emp_id=emp_id, start_time=start, stop_time=stop,
                                  hour_type=ht, remark=remark, case_number=sag)
            brugt, oprettet_id = ht, (_rh or {}).get("id")
            break
        except Exception as e:   # proev naeste type (baade rettigheds- og serverfejl paa en type)
            t_navn = next((t.get("title") for t in typer if t.get("id") == ht), None) or f"id {ht}"
            fejl_pr_type.append((t_navn, str(e)[:300]))
    if brugt is None:
        # Diagnostik: ens fejl paa ALLE typer peger paa medarbejder-rettigheder ("Relevante
        # loen typer"); forskellige fejl peger paa felt-/API-problemer. Vis emp_id + case_id.
        unikke = list(dict.fromkeys(f for _, f in fejl_pr_type))
        if len(unikke) == 1:
            detalje = f"samme fejl for alle {len(fejl_pr_type)} typer: {unikke[0]}"
        else:
            detalje = "; ".join(f"{n}: {f}" for n, f in fejl_pr_type)
        fejl_txt = f"kunne ikke registrere timer (emp_id {emp_id}, sag-internt-id {cid}): {detalje}"
        print(f"[registrer_timer] {fejl_txt}", flush=True)   # fuld raa fejl i Railway-loggen
        return {"fejl": fejl_txt}
    brutto = round((stop - start) / 3600, 2)
    type_navn = next((t.get("title") for t in typer if t.get("id") == brugt), "")
    svar = f"Registreret {brutto} timer paa sag {sag} ({fra}-{til} den {dato})"
    if type_navn:
        svar += f", type: {type_navn}"
    if pause:
        svar += f", pause: {pause['quantity']} x {pause['navn']}"
    ud = {"resultat": svar}
    if oprettet_id:
        ud["_ref"] = {"type": "hour", "id": oprettet_id}
    return ud


def vis_raa_timer(args, ctx):
    """DEBUG: hent raa timelinjer fra ordrestyring, saa vi kan se hvilke hour_type-id'er
    systemet selv gemmer. Fuld JSON skrives til server-loggen (Railway)."""
    import json as _json
    rows = os_api.hours_raw()
    if not isinstance(rows, list):
        rows = [rows]
    sag = (args.get("sagsnummer") or "").strip()
    if sag:
        cid = os_gql._case_internal_id(sag)
        filtreret = [r for r in rows if str(r.get("case_id")) == str(cid)]
        if filtreret:
            rows = filtreret
    rows = rows[-5:]
    print(f"[vis_raa_timer] {_json.dumps(rows, ensure_ascii=False, default=str)[:3500]}", flush=True)
    return {"resultat": f"{len(rows)} timelinjer hentet. Den fulde raa data er skrevet til "
                        f"server-loggen. Vis brugeren felterne hour_type, emp_id og case_id ordret.",
            "timer": rows}


def sag_status(args, ctx):
    """Samlet overblik over en sag: hvad er udfyldt, hvor meget ligger der (timer,
    dokumenter, fakturaer), og hvad der mangler. Aura bruger det som guide."""
    sag = args["sagsnummer"]
    case = os_api.get_case(sag) or {}
    if not case:
        return {"fejl": f"kunne ikke finde sag {sag}"}
    gql = {}
    try:
        gql = os_gql.case_overview(sag)
    except Exception as e:
        print(f"[sag_status] gql-overblik fejlede: {str(e)[:200]}", flush=True)
    timer = []
    try:
        timer = [r for r in os_api.hours_raw() if str(r.get("new_case_number")) == str(sag)]
    except Exception as e:
        print(f"[sag_status] timer-opslag fejlede: {str(e)[:200]}", flush=True)
    timer_sum = round(sum((int(r.get("stop_time") or 0) - int(r.get("start_time") or 0))
                          for r in timer) / 3600, 2) if timer else 0
    kunde = None
    try:
        kunde = (os_api.get_debtor(case.get("customer_number")) or {}).get("customer_name")
    except Exception:
        pass
    ansvarlig = os_api.user_name(case.get("main_technician")) if case.get("main_technician") else None
    antal_dok = None
    try:
        antal_dok = os_gql.documentation_count(sag)   # Dokumentation-fanen (Auras fotos m.m.)
    except Exception as e:
        print(f"[sag_status] dokumentation-taeller fejlede: {str(e)[:200]}", flush=True)
    if antal_dok is None:
        antal_dok = gql.get("documentCount")
    else:
        antal_dok = (antal_dok or 0) + (gql.get("documentCount") or 0)
    antal_timer = len(timer) or (gql.get("hoursCount") or 0)
    status = {
        "sagsnummer": sag,
        "kunde": kunde,
        "beskrivelse": (case.get("description") or gql.get("description") or "")[:300] or None,
        "projektnavn": gql.get("projectName") or None,
        "reference": case.get("reference") or gql.get("reference") or None,
        "rekvisition": gql.get("requisition") or None,
        "ansvarlig": ansvarlig,
        "bemaerkninger": (case.get("remarks") or gql.get("remarks") or "")[:300] or None,
        "faerdigmelding": (case.get("work_done") or gql.get("workDone") or "")[:300] or None,
        "antal_dokumenter": antal_dok,
        "antal_timelinjer": antal_timer,
        "timer_i_alt": timer_sum or None,
        "antal_fakturaer": gql.get("salesInvoicesCount"),
        "lukket": os_api.is_closed(case),
    }
    mangler = []
    if not ansvarlig:
        mangler.append("ingen ansvarlig medarbejder tildelt")
    if not status["beskrivelse"]:
        mangler.append("ingen beskrivelse")
    if not status["reference"]:
        mangler.append("ingen reference")
    if not antal_timer:
        mangler.append("ingen timer registreret")
    if not (antal_dok or 0):
        mangler.append("ingen dokumenter/billeder")
    if not status["faerdigmelding"] and not status["lukket"]:
        mangler.append("ikke faerdigmeldt (ingen 'arbejde udfoert'-tekst)")
    status["mangler"] = mangler
    return status


def tildel_sag(args, ctx):
    """Tildel en sag til en medarbejder (saetter Ansvarlig). Kun leder."""
    sag = args["sagsnummer"]
    mids, ukendte = _find_medarbejdere(args, ctx, kraev=True)
    if ukendte:
        return {"resultat": "Jeg kunne ikke finde disse medarbejdere i ordrestyring: "
                            + ", ".join(ukendte) + ". Sig 'vis medarbejdere' for listen."}
    if not mids:
        return {"fejl": "jeg mangler medarbejdernes navne"}
    for mid in mids:
        os_gql.add_case_user(sag, mid)
    hvem = ", ".join(os_api.user_name(m) or str(m) for m in mids)
    return {"resultat": f"Sag {sag}: {hvem} er sat paa som medarbejder(e) (Medarbejdere-kortet)"}


def _find_medarbejdere(args, ctx, kraev=False):
    """Slaa en ELLER flere medarbejdere op ud fra args (medarbejder/medarbejdere).
    Returnerer (liste af id'er, liste af ukendte navne)."""
    navne = args.get("medarbejdere") or []
    if isinstance(navne, str):
        navne = [navne]
    if args.get("medarbejder"):
        navne = list(navne) + [args["medarbejder"]]
    navne = [str(n).strip() for n in navne if str(n).strip()]
    mids, ukendte = [], []
    for n in navne:
        mid = _find_user_id(n)
        if mid and mid not in mids:
            mids.append(mid)
        elif not mid:
            ukendte.append(n)
    if not navne and not kraev:
        mit = (db.get_user(ctx["telegram_id"]) or {}).get("os_user_id")
        if mit:
            mids = [mit]
    return mids, ukendte


def planlaeg_sag(args, ctx):
    """Planlaeg en sag: Planlagt tid med dato, tidsrum og medarbejder (kun leder)."""
    sag = args["sagsnummer"]
    dato = (args.get("dato") or "").strip() or now_local().strftime("%Y-%m-%d")

    def _norm_tid(s):
        s = str(s or "").strip().replace(".", ":")
        if not s:
            return None
        if ":" not in s:
            s = f"{int(s):02d}:00"
        return s

    try:
        fra = _norm_tid(args.get("fra"))
        til = _norm_tid(args.get("til"))
    except (ValueError, TypeError):
        return {"fejl": "kunne ikke forstaa klokkeslaettet"}
    if not fra:
        return {"fejl": "jeg mangler et starttidspunkt (fx kl. 8)"}
    if not til:
        h, m = fra.split(":")
        til = f"{min(23, int(h) + 1):02d}:{m}"
    try:
        start = _unix_ts(dato, fra)
        stop = _unix_ts(dato, til)
    except ValueError:
        return {"fejl": "kunne ikke forstaa dato eller klokkeslaet"}
    if stop <= start:
        return {"fejl": "sluttidspunktet skal vaere efter starttidspunktet"}
    mids, ukendte = _find_medarbejdere(args, ctx)
    if ukendte:
        return {"resultat": "Jeg kunne ikke finde disse medarbejdere i ordrestyring: "
                            + ", ".join(ukendte) + ". Sig 'vis medarbejdere' for listen."}
    if not mids:
        return {"fejl": "sig hvilke medarbejdere der skal planlaegges paa sagen"}
    # Findes der allerede en plan for SAMME medarbejder SAMME dag, erstattes den
    # (saa "tilfoej/ret planen" aldrig giver dubletter)
    erstattet = 0
    try:
        evts = os_gql.planned_events(sag)
        print(f"[planlaeg_sag] sag {sag}: {len(evts)} eksisterende planer; soeger dato={dato}, "
              f"medarbejder-id'er={mids}", flush=True)
        for ev in evts:
            u = (ev.get("user") or {}).get("id")
            try:
                ev_dato = datetime.fromtimestamp(int(ev.get("startTime") or 0)).strftime("%Y-%m-%d")
            except (ValueError, TypeError, OSError):
                ev_dato = "?"
            print(f"[planlaeg_sag] kandidat: id={ev.get('id')} dato={ev_dato} bruger={u}", flush=True)
            if ev_dato != dato or not ev.get("id"):
                continue
            # samme dag: slet hvis en af de valgte medarbejdere ELLER hvis brugeren ikke kan aflaeses
            if u is None or int(u) in [int(m) for m in mids]:
                os_gql.delete_event(ev["id"])
                erstattet += 1
                print(f"[planlaeg_sag] slettede plan id={ev.get('id')}", flush=True)
    except Exception as e:
        print(f"[planlaeg_sag] kunne ikke rydde gamle planer: {str(e)[:200]}", flush=True)
    res = os_gql.create_planned_event(sag, mids, start, stop, text=(args.get("beskrivelse") or None))
    hvem = ", ".join(os_api.user_name(m) or str(m) for m in mids)
    tekst = f"Sag {sag} er planlagt {dato} kl. {fra}-{til} med {hvem}."
    if erstattet:
        tekst = f"Planen er OPDATERET (den gamle blev erstattet): {tekst}"
    tekst += (" Den vises i Planlagt tid og Dagsoversigten, og status skifter selv til "
              "Igangvaerende naar tiden naas.")
    ud = {"resultat": tekst}
    if isinstance(res, dict) and res.get("id"):
        ud["_ref"] = {"type": "event", "id": res["id"]}
    return ud


def vis_handlinger(args, ctx):
    """Handlingsloggen (kun leder): hvad Aura har udfoert, af hvem og hvornaar."""
    dato = (args.get("dato") or "").strip() or None
    try:
        antal = min(100, int(args.get("antal") or 30))
    except (ValueError, TypeError):
        antal = 30
    rows = db.handlinger_seneste(antal=antal, dato=dato)
    return {"antal": len(rows), "handlinger": [
        {"tid": r.get("ts"), "bruger": r.get("navn") or "Aura (automatisk)",
         "handling": r.get("handling"), "detaljer": r.get("detaljer")}
        for r in rows]}


def fortryd_handling(args, ctx):
    """Fortryd Auras seneste handling (timer/vare/foto). Leder maa fortryde alt;
    medarbejder kun egne handlinger. Valgfrit: sagsnummer og/eller type."""
    sag = str(args.get("sagsnummer") or "").strip()
    typ = (args.get("type") or "").strip().lower()[:3]   # 'tim'|'var'|'fot'
    DTYPE = {"hour": "timeregistrering", "material": "vare", "dokument": "foto",
             "event": "planlagt tid"}
    kandidat = None
    for r in db.handlinger_seneste(antal=100):
        if not r.get("ref_id") or r.get("fortrudt"):
            continue
        if ctx.get("rolle") != "pro" and str(r.get("telegram_id")) != str(ctx.get("telegram_id")):
            continue
        dnavn = DTYPE.get(r.get("ref_type") or "")
        if not dnavn:
            continue
        if typ and not (dnavn.startswith(typ) or (typ == "tim" and r.get("ref_type") == "hour")):
            continue
        if sag and f'"{sag}"' not in (r.get("detaljer") or "") and f"sag {sag}" not in (r.get("detaljer") or ""):
            continue
        kandidat = r
        break
    if not kandidat:
        return {"resultat": "Jeg fandt ingen handling der kan fortrydes. Kun timeregistreringer, "
                            "varer og fotos som Aura selv har oprettet, kan fortrydes."}
    rt, rid = kandidat["ref_type"], kandidat["ref_id"]
    try:
        if rt == "hour":
            os_gql.delete_hour(rid)
        elif rt == "material":
            os_gql.delete_case_material(rid)
        elif rt == "dokument":
            os_gql.delete_documentation_file(rid)
        elif rt == "event":
            os_gql.delete_event(rid)
    except Exception as e:
        print(f"[fortryd] {rt} {rid} fejlede: {str(e)[:250]}", flush=True)
        return {"fejl": f"kunne ikke fortryde ({DTYPE.get(rt)}): {str(e)[:150]}"}
    db.marker_fortrudt(kandidat["id"])
    try:
        db.log_handling(ctx.get("telegram_id"), ctx.get("navn"), ctx.get("rolle"),
                        "handling fortrudt", f"{kandidat['handling']}: {(kandidat.get('detaljer') or '')[:200]}")
    except Exception:
        pass
    return {"resultat": f"Fortrudt: {kandidat['handling']} ({(kandidat.get('detaljer') or '')[:150]}). "
                        f"Oprindeligt udført af {kandidat.get('navn') or 'ukendt'} kl. {(kandidat.get('ts') or '')[11:16]}."}


# ---------- registry: skema + funktion + tilladte roller ----------

TOOLS = [
    {
        "func": vis_raa_timer, "roles": {"pro"},
        "schema": {"type": "function", "function": {
            "name": "vis_raa_timer",
            "description": "DEBUG (kun leder): hent raa timelinjer fra ordrestyring med de tekniske "
                           "felter (hour_type, emp_id, case_id). Bruges naar brugeren siger 'vis raa "
                           "timer'. Valgfrit: sagsnummer for kun at se en bestemt sags timer.",
            "parameters": {"type": "object", "properties": {
                "sagsnummer": {"type": "string"}}, "required": []},
        }},
    },
    {
        "func": registrer_timer, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "registrer_timer",
            "description": "Registrer arbejdstimer paa en sag (Timer-fanen): hvor laenge arbejdet tog. "
                           "Angiv sagsnummer + fra og til som klokkeslaet (HH:MM). Valgfrit: dato (YYYY-MM-DD, "
                           "default i dag), type (time-type som 'normal'/'overtid'), beskrivelse, medarbejder "
                           "(navn - default den der spoerger), pause_min (pause i minutter) og tillaeg. "
                           "pause_min registreres som RIGTIG pause (firmaets pause-typer). Tillaeg "
                           "noteres i beskrivelsen. VIGTIGT: registrer ALDRIG samme tidsrum igen "
                           "for at tilfoeje pause bagefter - bed i stedet brugeren rette linjen i "
                           "ordrestyring (ellers opstaar dobbelt-linjer).",
            "parameters": {"type": "object", "properties": {
                "sagsnummer": {"type": "string"}, "fra": {"type": "string"}, "til": {"type": "string"},
                "dato": {"type": "string"}, "type": {"type": "string"},
                "beskrivelse": {"type": "string"}, "medarbejder": {"type": "string"},
                "pause_min": {"type": "integer"}, "tillaeg": {"type": "string"}},
                "required": ["sagsnummer", "fra", "til"]},
        }},
    },
    {
        "func": planlaeg_sag, "roles": {"pro"},
        "schema": {"type": "function", "function": {
            "name": "planlaeg_sag",
            "description": "Planlaeg en sag i kalenderen: opretter 'Planlagt tid' med dato, tidsrum og "
                           "medarbejder, og laegger samtidig medarbejderen paa sagen. Brug ved fx "
                           "'planlaeg sag 124 til i morgen kl. 8 med Dmitri'. fra/til som HH:MM, dato "
                           "som YYYY-MM-DD (default i dag). Uden medarbejder bruges den der spoerger. "
                           "Retter/tilfoejer brugeren noget til en plan, saa kald bare planlaeg_sag "
                           "igen med de fulde tider - den gamle plan erstattes automatisk (ingen dublet).",
            "parameters": {"type": "object", "properties": {
                "sagsnummer": {"type": "string"}, "dato": {"type": "string"},
                "fra": {"type": "string"}, "til": {"type": "string"},
                "medarbejdere": {"type": "array", "items": {"type": "string"},
                                 "description": "et eller flere medarbejder-navne"},
                "beskrivelse": {"type": "string"}},
                "required": ["sagsnummer", "fra"]},
        }},
    },
    {
        "func": tildel_sag, "roles": {"pro"},
        "schema": {"type": "function", "function": {
            "name": "tildel_sag",
            "description": "Tildel en sag til en MEDARBEJDER saa den staar som Ansvarlig og dukker op "
                           "under medarbejderens egne sager. Brug ALTID denne naar brugeren siger at en "
                           "medarbejder skal lave/udfoere/have en opgave (fx 'Dmitri skal lave det', "
                           "'giv sagen til Thomas'). Forveksl ALDRIG med kontaktperson - kontaktperson "
                           "er KUNDENS kontaktperson, aldrig en af firmaets medarbejdere.",
            "parameters": {"type": "object", "properties": {
                "sagsnummer": {"type": "string"},
                "medarbejdere": {"type": "array", "items": {"type": "string"},
                                 "description": "et eller flere medarbejder-navne"}},
                "required": ["sagsnummer", "medarbejdere"]},
        }},
    },
    {
        "func": fortryd_handling, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "fortryd_handling",
            "description": "Fortryd/annullér Auras seneste handling: sletter den timeregistrering, "
                           "vare eller det foto Aura selv har oprettet. Brug ved 'fortryd', 'slet den "
                           "sidste registrering', 'det var en fejl, fjern den igen'. Valgfrit: "
                           "sagsnummer og type ('timer'/'vare'/'foto') for at ramme praecist. "
                           "Medarbejdere kan kun fortryde egne handlinger.",
            "parameters": {"type": "object", "properties": {
                "sagsnummer": {"type": "string"}, "type": {"type": "string"}},
                "required": []},
        }},
    },
    {
        "func": vis_handlinger, "roles": {"pro"},
        "schema": {"type": "function", "function": {
            "name": "vis_handlinger",
            "description": "Handlingslog (kun leder): liste over alt Aura har udfoert (timer, sager, "
                           "varer, fotos, rykkere, automatiske statusskift) med bruger og tidspunkt. "
                           "Brug ved 'hvad har du lavet i dag', 'vis handlingsloggen', 'hvad er der "
                           "sket', 'hvem registrerede timer paa sag X'. Valgfrit: dato (YYYY-MM-DD), "
                           "antal (default 30).",
            "parameters": {"type": "object", "properties": {
                "dato": {"type": "string"}, "antal": {"type": "integer"}},
                "required": []},
        }},
    },
    {
        "func": sag_status, "roles": {"pro", "jun"},
        "schema": {"type": "function", "function": {
            "name": "sag_status",
            "description": "Samlet status/overblik paa en sag: hvad er udfyldt (kunde, beskrivelse, "
                           "projektnavn, reference, ansvarlig, bemaerkninger, faerdigmelding), hvor "
                           "meget der ligger paa den (antal timer/timelinjer, dokumenter/billeder, "
                           "fakturaer) og en 'mangler'-liste. Brug ved spoergsmaal som 'hvad er status "
                           "paa sag X', 'hvad mangler paa sagen', 'giv mig overblik over sag X', 'er "
                           "sagen klar til fakturering'. Naevn kun de vigtigste mangler for brugeren.",
            "parameters": {"type": "object", "properties": {
                "sagsnummer": {"type": "string"}}, "required": ["sagsnummer"]},
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
                           "Valgfrit: projektnavn, reference, kontaktperson (KUNDENS kontaktperson - ALDRIG en "
                           "medarbejder; skal en medarbejder have opgaven, brug tildel_sag bagefter), "
                           "leveringsadresse. Returnerer sagsnummer.",
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


# Ændrende værktøjer der skal i handlingsloggen (læse-værktøjer logges ikke)
MUTERENDE = {
    "opret_kunde": "kunde oprettet", "opdater_kunde": "kunde opdateret",
    "opret_sag": "sag oprettet", "opdater_sag": "sag opdateret",
    "skriv_bemaerkning": "bemærkning skrevet", "afslut_sag": "sag færdigmeldt",
    "registrer_timer": "timer registreret", "tilfoej_vare": "vare tilføjet",
    "send_paamindelse_email": "rykker sendt", "saet_rykker_niveau": "rykker-tæller sat",
    "husk_aftale": "aftale gemt", "tildel_sag": "sag tildelt", "planlaeg_sag": "sag planlagt",
}


def call_tool(name: str, args: dict, ctx: dict):
    tool = BY_NAME.get(name)
    if not tool:
        return {"fejl": f"ukendt værktøj {name}"}
    if ctx["rolle"] not in tool["roles"]:
        return {"fejl": "afvist: kun lederen kan bruge denne funktion"}
    if (name in MUTERENDE or name == "fortryd_handling") and db.get_meta("aura_pauseret") == "1":
        return {"resultat": "Aura er sat på pause af lederen, så jeg må ikke udføre ændringer lige nu. "
                            "Læsning og søgning virker stadig. Lederen starter mig igen med 'aura start'."}
    # LØBSK-BREMSE: for mange ændringer fra én bruger på kort tid -> Aura pauser sig selv
    if name in MUTERENDE:
        try:
            MAX_PR_TIME = 15
            n = db.antal_handlinger_seneste_time(ctx.get("telegram_id"))
            if n >= MAX_PR_TIME:
                db.set_meta("aura_pauseret", "1")
                db.log_handling(ctx.get("telegram_id"), ctx.get("navn"), ctx.get("rolle"),
                                "LØBSK-BREMSE udløst", f"{n} ændringer på 1 time -> Aura pauset")
                print(f"[bremse] {ctx.get('navn')} naaede {n} aendringer/time -> pause", flush=True)
                try:
                    from .config import LEADER_GROUP_CHAT_ID
                    from . import telegram as _tg
                    if LEADER_GROUP_CHAT_ID:
                        _tg.send_message(LEADER_GROUP_CHAT_ID,
                                         f"🛑 Aura har sat sig selv på pause: {ctx.get('navn')} har lavet "
                                         f"{n} ændringer på 1 time (grænse {MAX_PR_TIME}). Tjek handlingsloggen "
                                         "og skriv 'aura start' for at fortsætte.")
                except Exception:
                    pass
                return {"resultat": "Jeg har sat mig selv på pause som sikkerhed: der er lavet usædvanligt "
                                    "mange ændringer på kort tid. Lederen er informeret og kan starte mig "
                                    "igen med 'aura start'."}
        except Exception:
            pass   # bremse-fejl må aldrig blokere normal drift
    try:
        res = tool["func"](args, ctx)
    except Exception as e:  # ægte fejl -> agenten fortæller ærligt at det fejlede
        return {"fejl": str(e)}
    ref = res.pop("_ref", None) if isinstance(res, dict) else None
    if name in MUTERENDE and isinstance(res, dict) and not res.get("fejl"):
        try:
            import json as _json
            db.log_handling(ctx.get("telegram_id"), ctx.get("navn"), ctx.get("rolle"),
                            MUTERENDE[name], _json.dumps(args, ensure_ascii=False)[:350],
                            ref_type=(ref or {}).get("type"), ref_id=(ref or {}).get("id"))
        except Exception:
            pass   # log-fejl må aldrig vælte selve handlingen
    return res
# slut

"""SQLite: brugere/roller, rykker-tællere og aftaler (erstatter Make Data store + kalender)."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from .config import DB_PATH


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS brugere (
                telegram_id TEXT PRIMARY KEY,
                navn        TEXT NOT NULL,
                rolle       TEXT NOT NULL DEFAULT 'jun',   -- 'pro' (leder) | 'jun' (medarbejder)
                os_user_id  TEXT,                          -- medarbejderens ordrestyring-id (til 'egne sager')
                aktiv       INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS rykkere (
                kundenummer TEXT PRIMARY KEY,
                antal       INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS aftaler (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id TEXT NOT NULL,
                kunde       TEXT,
                opgave      TEXT,
                start       TEXT NOT NULL,     -- ISO: 2026-06-13T10:00:00
                mindet      INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS samtaler (
                telegram_id TEXT NOT NULL,
                rolle       TEXT NOT NULL,     -- 'user' | 'assistant' | 'tool'
                indhold     TEXT NOT NULL,
                ts          DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS handlinger (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,                 -- lokal tid, ISO
                telegram_id TEXT,
                navn        TEXT,
                rolle       TEXT,
                handling    TEXT NOT NULL,
                detaljer    TEXT,
                ref_type    TEXT,                          -- 'hour'|'material'|'dokument' (til fortryd)
                ref_id      TEXT,                          -- objektets id i ordrestyring
                fortrudt    INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS adr_anmodninger (
                token      TEXT PRIMARY KEY,
                telefon    TEXT,
                navn       TEXT,
                adresse    TEXT,
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS meta (
                k TEXT PRIMARY KEY,
                v TEXT
            );
            CREATE TABLE IF NOT EXISTS ref_anmodninger (
                token            TEXT PRIMARY KEY,
                case_number      TEXT NOT NULL,
                customer_number  TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'done'
                reference        TEXT,
                antal_mails      INTEGER NOT NULL DEFAULT 0,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                sidste_mail_at   DATETIME,
                filled_at        DATETIME
            );
            """
        )
        # Migration: tilføj os_user_id til eksisterende databaser (ignoreres hvis den findes)
        try:
            c.execute("ALTER TABLE brugere ADD COLUMN os_user_id TEXT")
        except sqlite3.OperationalError:
            pass
        # Migration: kilde-kolonne på brugere (env = SEED_USERS, admin = dashboard)
        try:
            c.execute("ALTER TABLE brugere ADD COLUMN kilde TEXT NOT NULL DEFAULT 'env'")
        except sqlite3.OperationalError:
            pass
        # Migration: fortryd-kolonner på handlinger
        for kol in ("ref_type TEXT", "ref_id TEXT", "fortrudt INTEGER NOT NULL DEFAULT 0"):
            try:
                c.execute(f"ALTER TABLE handlinger ADD COLUMN {kol}")
            except sqlite3.OperationalError:
                pass


# ---- Brugere / roller ----

def get_user(telegram_id: str):
    with conn() as c:
        row = c.execute(
            "SELECT * FROM brugere WHERE telegram_id=? AND aktiv=1", (str(telegram_id),)
        ).fetchone()
        return dict(row) if row else None


def upsert_user(telegram_id, navn, rolle="jun", os_user_id=None, kilde="env"):
    with conn() as c:
        c.execute(
            "INSERT INTO brugere(telegram_id, navn, rolle, os_user_id, aktiv, kilde) VALUES(?,?,?,?,1,?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET navn=excluded.navn, rolle=excluded.rolle, "
            "os_user_id=COALESCE(excluded.os_user_id, brugere.os_user_id), aktiv=1, kilde=excluded.kilde",
            (str(telegram_id), navn, rolle, str(os_user_id) if os_user_id else None, kilde),
        )


def deactivate_user(telegram_id):
    with conn() as c:
        c.execute("UPDATE brugere SET aktiv=0 WHERE telegram_id=?", (str(telegram_id),))


def deactivate_users_not_in(keep_ids):
    """Fjern adgang (aktiv=0) for alle brugere der IKKE står på listen. Gør SEED_USERS
    til den fulde sandhed, så man kan fjerne adgang ved at fjerne nogen fra listen."""
    keep = [str(i) for i in keep_ids]
    if not keep:
        return
    placeholders = ",".join("?" for _ in keep)
    with conn() as c:
        c.execute(f"UPDATE brugere SET aktiv=0 WHERE kilde='env' AND telegram_id NOT IN ({placeholders})",
                  keep)


def all_users():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM brugere WHERE aktiv=1").fetchall()]


def seed_users_from_env():
    """Opretter brugere fra miljøvariablen SEED_USERS.
    Format: 'telegram_id:navn:rolle:os_user_id, ...' (rolle = pro|jun, os_user_id valgfrit).
    os_user_id er medarbejderens ordrestyring-id (bruges til 'kun egne sager').
    Fx: 7713099063:Dan:pro,6779276258:Sahab:jun:118
    """
    import os
    raw = os.environ.get("SEED_USERS", "")
    keep = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        bits = [b.strip() for b in part.split(":")]
        if len(bits) >= 2:
            tid, navn = bits[0], bits[1]
            rolle = bits[2] if len(bits) > 2 else "jun"
            os_user_id = bits[3] if len(bits) > 3 and bits[3] else None
            upsert_user(tid, navn, rolle, os_user_id)
            keep.add(str(tid))
    # SEED_USERS er den fulde sandhed: alle andre mister adgang (kun hvis listen ikke er tom)
    deactivate_users_not_in(keep)


# ---- Rykker-tæller (eskalering 1->2->3) ----

def next_reminder_level(kundenummer: str) -> int:
    with conn() as c:
        row = c.execute("SELECT antal FROM rykkere WHERE kundenummer=?", (str(kundenummer),)).fetchone()
        level = min(3, (row["antal"] if row else 0) + 1)
        c.execute(
            "INSERT INTO rykkere(kundenummer, antal) VALUES(?,?) "
            "ON CONFLICT(kundenummer) DO UPDATE SET antal=excluded.antal",
            (str(kundenummer), level),
        )
        return level


def get_reminder_count(kundenummer: str) -> int:
    with conn() as c:
        row = c.execute("SELECT antal FROM rykkere WHERE kundenummer=?", (str(kundenummer),)).fetchone()
        return row["antal"] if row else 0


def set_reminder_count(kundenummer: str, antal: int):
    """Lederen kan synkronisere tælleren med manuelle rykkere."""
    with conn() as c:
        c.execute(
            "INSERT INTO rykkere(kundenummer, antal) VALUES(?,?) "
            "ON CONFLICT(kundenummer) DO UPDATE SET antal=excluded.antal",
            (str(kundenummer), int(antal)),
        )


# ---- Aftaler (personlig hukommelse) ----

def add_appointment(telegram_id, kunde, opgave, start_iso):
    with conn() as c:
        # Undgå dubletter: samme bruger + (næsten) samme opgave + start inden for 3 min.
        # Fuzzy-sammenligning fanger tale-varianter som "Ring til Thomas"/"Ringe til Thomas".
        try:
            import difflib
            ny = datetime.fromisoformat(start_iso)
            for row in c.execute(
                "SELECT start, opgave FROM aftaler WHERE telegram_id=?", (str(telegram_id),)
            ).fetchall():
                try:
                    if abs((datetime.fromisoformat(row["start"]) - ny).total_seconds()) > 180:
                        continue
                    a = (row["opgave"] or "").strip().lower()
                    b = (opgave or "").strip().lower()
                    if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
                        return   # dublet -> opret ikke igen
                except (ValueError, TypeError):
                    pass
        except (ValueError, TypeError):
            pass
        c.execute(
            "INSERT INTO aftaler(telegram_id, kunde, opgave, start) VALUES(?,?,?,?)",
            (str(telegram_id), kunde, opgave, start_iso),
        )


def appointments_between(telegram_id, fra_iso, til_iso):
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM aftaler WHERE telegram_id=? AND start>=? AND start<=? ORDER BY start",
            (str(telegram_id), fra_iso, til_iso),
        ).fetchall()
        return [dict(r) for r in rows]


def due_reminders(fra_iso, til_iso):
    """Aftaler (alle brugere) der starter i [fra, til] og IKKE er mindet om endnu."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM aftaler WHERE mindet=0 AND start>=? AND start<=? ORDER BY start",
            (fra_iso, til_iso),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_reminded(aftale_id):
    with conn() as c:
        c.execute("UPDATE aftaler SET mindet=1 WHERE id=?", (aftale_id,))


# ---- Kort samtale-hukommelse (til opklarende dialog) ----

def recent_messages(telegram_id, limit=10):
    with conn() as c:
        rows = c.execute(
            "SELECT rolle, indhold FROM samtaler WHERE telegram_id=? ORDER BY ts DESC LIMIT ?",
            (str(telegram_id), limit),
        ).fetchall()
        return [{"role": r["rolle"], "content": r["indhold"]} for r in reversed(rows)]


def save_message(telegram_id, rolle, indhold):
    with conn() as c:
        c.execute(
            "INSERT INTO samtaler(telegram_id, rolle, indhold) VALUES(?,?,?)",
            (str(telegram_id), rolle, indhold),
        )


# ---- Handlingslog (leder-kontrol: hvad har Aura udført?) ----

def log_handling(telegram_id, navn, rolle, handling, detaljer="", ref_type=None, ref_id=None):
    """Registrér en udført handling. Må ALDRIG vælte den egentlige handling -> try/except hos kalderen."""
    with conn() as c:
        c.execute(
            "INSERT INTO handlinger(ts, telegram_id, navn, rolle, handling, detaljer, ref_type, ref_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), str(telegram_id or ""),
             navn or "", rolle or "", handling, (detaljer or "")[:400],
             ref_type, str(ref_id) if ref_id is not None else None),
        )


def antal_handlinger_seneste_time(telegram_id):
    """Antal ændrende handlinger fra én bruger den seneste time (til løbsk-bremsen)."""
    from datetime import timedelta
    graense = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM handlinger WHERE telegram_id=? AND ts>=? "
            "AND handling NOT IN ('Aura sat på pause', 'Aura startet igen')",
            (str(telegram_id or ""), graense)).fetchone()
        return int(row["n"] if row else 0)


def marker_fortrudt(handling_id):
    with conn() as c:
        c.execute("UPDATE handlinger SET fortrudt=1 WHERE id=?", (int(handling_id),))


def handlinger_seneste(antal=30, dato=None):
    """Seneste handlinger, nyeste først. dato='YYYY-MM-DD' begrænser til én dag."""
    with conn() as c:
        if dato:
            rows = c.execute(
                "SELECT * FROM handlinger WHERE substr(ts,1,10)=? ORDER BY ts DESC LIMIT ?",
                (dato, int(antal))).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM handlinger ORDER BY ts DESC LIMIT ?", (int(antal),)).fetchall()
        return [dict(r) for r in rows]


def samtaler_seneste(antal=300):
    """Seneste samtale-beskeder paa tvaers af brugere (til Pilly-overvaagning)."""
    with conn() as c:
        rows = c.execute(
            "SELECT telegram_id, rolle, indhold, ts FROM samtaler ORDER BY rowid DESC LIMIT ?",
            (int(antal),)).fetchall()
        return [dict(r) for r in rows]


# ---- Meta (nøgle/værdi) + "sidst sete ordre" ----

def get_meta(k, default=None):
    with conn() as c:
        row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default


def set_meta(k, v):
    with conn() as c:
        c.execute("INSERT INTO meta(k, v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def funktion_til(navn):
    """Er en funktion taendt? (Pilly-kontakter; alt er TIL medmindre eksplicit slaaet fra)."""
    return get_meta("funk_" + navn) != "0"


def graense(navn, standard):
    """Justerbar graense fra dashboardet (fx loebsk-bremse)."""
    try:
        return int(get_meta("graense_" + navn) or standard)
    except (TypeError, ValueError):
        return standard


def get_last_seen_order(telegram_id):
    v = get_meta(f"sidst_ordre:{telegram_id}")
    return int(v) if v else 0


def set_last_seen_order(telegram_id, ts):
    set_meta(f"sidst_ordre:{telegram_id}", int(ts))


# ---- Adresse-anmodninger (telefon-agentens SMS-link) ----

def create_adr_request(token, telefon, navn=""):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO adr_anmodninger(token, telefon, navn) VALUES(?,?,?)",
                  (token, str(telefon or ""), navn or ""))


def get_adr_request(token):
    with conn() as c:
        row = c.execute("SELECT * FROM adr_anmodninger WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None


def mark_adr_done(token, navn, adresse):
    with conn() as c:
        c.execute("UPDATE adr_anmodninger SET status='done', navn=?, adresse=? WHERE token=?",
                  (navn or "", adresse or "", token))


# ---- Referenceanmodninger (kundeportal) ----

def create_ref_request(token, case_number, customer_number):
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO ref_anmodninger(token, case_number, customer_number) "
            "VALUES(?,?,?)", (token, str(case_number), str(customer_number)),
        )


def get_ref_request(token):
    with conn() as c:
        row = c.execute("SELECT * FROM ref_anmodninger WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None


def get_ref_request_by_case(case_number):
    with conn() as c:
        row = c.execute("SELECT * FROM ref_anmodninger WHERE case_number=?",
                        (str(case_number),)).fetchone()
        return dict(row) if row else None


def pending_ref_requests():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM ref_anmodninger WHERE status='pending'").fetchall()]


def mark_ref_mailed(token):
    with conn() as c:
        c.execute("UPDATE ref_anmodninger SET antal_mails = antal_mails + 1, "
                  "sidste_mail_at = CURRENT_TIMESTAMP WHERE token=?", (token,))


def mark_ref_done(token, reference):
    with conn() as c:
        c.execute("UPDATE ref_anmodninger SET status='done', reference=?, "
                  "filled_at=CURRENT_TIMESTAMP WHERE token=?", (reference, token))


def mark_ref_done_by_case(case_number, reference=None):
    """Markér som udfyldt hvis referencen er kommet ind ad anden vej (fx direkte i ordrestyring)."""
    with conn() as c:
        c.execute("UPDATE ref_anmodninger SET status='done', reference=COALESCE(?, reference), "
                  "filled_at=CURRENT_TIMESTAMP WHERE case_number=? AND status='pending'",
                  (reference, str(case_number)))

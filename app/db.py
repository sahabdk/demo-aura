"""SQLite: brugere/roller, rykker-tællere og aftaler (erstatter Make Data store + kalender)."""
import sqlite3
from contextlib import contextmanager
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


# ---- Brugere / roller ----

def get_user(telegram_id: str):
    with conn() as c:
        row = c.execute(
            "SELECT * FROM brugere WHERE telegram_id=? AND aktiv=1", (str(telegram_id),)
        ).fetchone()
        return dict(row) if row else None


def upsert_user(telegram_id, navn, rolle="jun", os_user_id=None):
    with conn() as c:
        c.execute(
            "INSERT INTO brugere(telegram_id, navn, rolle, os_user_id) VALUES(?,?,?,?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET navn=excluded.navn, rolle=excluded.rolle, "
            "os_user_id=COALESCE(excluded.os_user_id, brugere.os_user_id)",
            (str(telegram_id), navn, rolle, str(os_user_id) if os_user_id else None),
        )


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


# ---- Meta (nøgle/værdi) + "sidst sete ordre" ----

def get_meta(k, default=None):
    with conn() as c:
        row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default


def set_meta(k, v):
    with conn() as c:
        c.execute("INSERT INTO meta(k, v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def get_last_seen_order(telegram_id):
    v = get_meta(f"sidst_ordre:{telegram_id}")
    return int(v) if v else 0


def set_last_seen_order(telegram_id, ts):
    set_meta(f"sidst_ordre:{telegram_id}", int(ts))


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

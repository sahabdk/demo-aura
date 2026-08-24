"""Central konfiguration. Læser miljøvariabler fra .env."""
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
OPENAI_TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")  # naturlig, klar tale
OPENAI_TTS_VOICE = os.environ.get("OPENAI_TTS_VOICE", "nova")    # stemme (alloy/echo/fable/onyx/nova/shimmer)
OPENAI_TTS_INSTRUCTIONS = os.environ.get(
    "OPENAI_TTS_INSTRUCTIONS",
    "Tal tydeligt, roligt og venligt på dansk. Udtal tal og adresser klart.")
OPENAI_STT_MODEL = os.environ.get("OPENAI_STT_MODEL", "gpt-4o-transcribe")  # bedre dansk-forståelse
# Reasoning-niveau for gpt-5/o-modeller: minimal|low|medium|high. Lavt = hurtigere svar.
OPENAI_REASONING = os.environ.get("OPENAI_REASONING", "low")

ORDRESTYRING_KEY = os.environ.get("ORDRESTYRING_KEY", "")
ORDRESTYRING_BASE = os.environ.get("ORDRESTYRING_BASE", "https://v2.api.ordrestyring.dk")
# GraphQL-API (varesøgning + tilføj materiale). Bruger samme nøgle som v2 medmindre andet sættes.
OS_GRAPHQL_URL = os.environ.get("OS_GRAPHQL_URL", "https://graphql.ordrestyring.dk/graphql")
OS_GRAPHQL_KEY = os.environ.get("OS_GRAPHQL_KEY", ORDRESTYRING_KEY)

LEADER_GROUP_CHAT_ID = os.environ.get("LEADER_GROUP_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "aura.db")
TZ = os.environ.get("TZ", "Europe/Copenhagen")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def now_local():
    """Nuværende tid i firmaets tidszone som NAIV datetime (wall-clock), så den matcher
    de aftale-tidspunkter agenten gemmer. Bruges af både agent og scheduler."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ)).replace(tzinfo=None)
    except Exception:
        return datetime.now()

# --- Referencenummer-portal ---
# Komma-liste over de store kunders kundenumre (kun disse jagtes for manglende reference)
# Format: "90210,90500" ELLER med fast referencemail pr. kunde: "90210;navn@firma.dk,90500"
# (email efter ';' overstyrer kundekortets mail - udeladt = kundekortet bruges)
REF_CUSTOMERS = []
REF_EMAILS = {}
for _r in os.environ.get("REF_CUSTOMERS", "").split(","):
    _r = _r.strip()
    if not _r:
        continue
    if ";" in _r:
        _nr, _mail = _r.split(";", 1)
        _nr, _mail = _nr.strip(), _mail.strip()
        if _nr:
            REF_CUSTOMERS.append(_nr)
            if "@" in _mail:
                REF_EMAILS[_nr] = _mail
    else:
        REF_CUSTOMERS.append(_r)
# Offentlig URL til appen (bruges til at bygge portal-links), fx https://aura-production.up.railway.app
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
if APP_BASE_URL and not APP_BASE_URL.startswith("http"):
    APP_BASE_URL = "https://" + APP_BASE_URL   # taal at variablen er sat uden https://
# Påmindelse: send igen efter X dage hvis kunden ikke har udfyldt, maks Y mails i alt
REF_REMINDER_DAGE = int(os.environ.get("REF_REMINDER_DAGE", "3"))
REF_MAX_MAILS = int(os.environ.get("REF_MAX_MAILS", "2"))
# Hvor mange dage tilbage scanningen kigger efter sager (created_at-filter)
REF_SCAN_DAGE = int(os.environ.get("REF_SCAN_DAGE", "14"))

# e-conomic (forfaldne fakturaer fra bogholderiet)
ECONOMIC_APP_TOKEN = os.getenv("ECONOMIC_APP_TOKEN", "")
ECONOMIC_GRANT_TOKEN = os.getenv("ECONOMIC_GRANT_TOKEN", "")

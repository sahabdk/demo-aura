"""Central konfiguration. Læser miljøvariabler fra .env."""
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")

ORDRESTYRING_KEY = os.environ.get("ORDRESTYRING_KEY", "")
ORDRESTYRING_BASE = os.environ.get("ORDRESTYRING_BASE", "https://v2.api.ordrestyring.dk")

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
REF_CUSTOMERS = [c.strip() for c in os.environ.get("REF_CUSTOMERS", "").split(",") if c.strip()]
# Offentlig URL til appen (bruges til at bygge portal-links), fx https://aura-production.up.railway.app
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
# Påmindelse: send igen efter X dage hvis kunden ikke har udfyldt, maks Y mails i alt
REF_REMINDER_DAGE = int(os.environ.get("REF_REMINDER_DAGE", "3"))
REF_MAX_MAILS = int(os.environ.get("REF_MAX_MAILS", "2"))
# Hvor mange dage tilbage scanningen kigger efter sager (created_at-filter)
REF_SCAN_DAGE = int(os.environ.get("REF_SCAN_DAGE", "14"))

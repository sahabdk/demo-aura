"""Central konfiguration. Læser miljøvariabler fra .env."""
import os
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

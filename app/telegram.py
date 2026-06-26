"""Telegram-hjælpere: send besked, hent + transskribér talebesked (Whisper)."""
import io
import requests
from openai import OpenAI
from .config import TELEGRAM_TOKEN, OPENAI_API_KEY

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}"
_client = OpenAI(api_key=OPENAI_API_KEY)


def send_message(chat_id, text: str):
    requests.get(f"{API}/sendMessage", params={"chat_id": chat_id, "text": text}, timeout=30)


def _download_voice(file_id: str) -> bytes:
    info = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=30).json()
    path = info["result"]["file_path"]
    return requests.get(f"{FILE_API}/{path}", timeout=60).content


def transcribe_voice(file_id: str) -> str:
    audio = io.BytesIO(_download_voice(file_id))
    audio.name = "voice.ogg"
    tr = _client.audio.transcriptions.create(
        model="whisper-1", file=audio, language="da",
        prompt="Dansk talebesked om en VVS-sag. Indeholder ofte ordet 'sag' efterfulgt af et tal, samt kundenavne og adresser.",
    )
    return tr.text

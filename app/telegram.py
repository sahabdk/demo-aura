"""Telegram-hjælpere: send besked, hent + transskribér talebesked (Whisper), tale (TTS)."""
import io
import re
import requests
from openai import OpenAI
from .config import (TELEGRAM_TOKEN, OPENAI_API_KEY, OPENAI_TTS_MODEL, OPENAI_TTS_VOICE,
                     OPENAI_TTS_INSTRUCTIONS, OPENAI_STT_MODEL)

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}"
_client = OpenAI(api_key=OPENAI_API_KEY)


def send_message(chat_id, text: str):
    requests.get(f"{API}/sendMessage", params={"chat_id": chat_id, "text": text}, timeout=30)


def send_chat_action(chat_id, action: str = "typing"):
    """Vis 'skriver…' (eller 'optager…') mens vi arbejder — bedre oplevet svartid."""
    try:
        requests.post(f"{API}/sendChatAction", json={"chat_id": chat_id, "action": action}, timeout=10)
    except Exception:
        pass


def _keyboard(rows):
    """rows = [[(tekst, callback_data), ...], ...] -> Telegram inline keyboard."""
    return {"inline_keyboard": [
        [{"text": t, "callback_data": cb} for (t, cb) in row] for row in rows
    ]}


def send_buttons(chat_id, text: str, rows):
    requests.post(f"{API}/sendMessage", json={
        "chat_id": chat_id, "text": text, "reply_markup": _keyboard(rows)
    }, timeout=30)


def edit_message(chat_id, message_id, text: str, rows=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if rows is not None:
        payload["reply_markup"] = _keyboard(rows)
    requests.post(f"{API}/editMessageText", json=payload, timeout=30)


def answer_callback(callback_query_id, text: str = ""):
    requests.post(f"{API}/answerCallbackQuery", json={
        "callback_query_id": callback_query_id, "text": text
    }, timeout=30)


def send_and_get_id(chat_id, text: str):
    """Send en besked og returnér message_id (til at fastgøre/pinne)."""
    r = requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=30)
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None


def pin_message(chat_id, message_id):
    requests.post(f"{API}/pinChatMessage", json={
        "chat_id": chat_id, "message_id": message_id, "disable_notification": True,
    }, timeout=30)


def _download_voice(file_id: str) -> bytes:
    info = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=30).json()
    path = info["result"]["file_path"]
    return requests.get(f"{FILE_API}/{path}", timeout=60).content


def download_file(file_id: str) -> bytes:
    """Hent en vilkaarlig fil (fx et foto) fra Telegram."""
    return _download_voice(file_id)


def transcribe_voice(file_id: str) -> str:
    audio = io.BytesIO(_download_voice(file_id))
    audio.name = "voice.ogg"
    tr = _client.audio.transcriptions.create(
        model=OPENAI_STT_MODEL, file=audio, language="da",
        prompt="Dansk talebesked om en VVS-/el-sag. Indeholder ofte ordet 'sag' efterfulgt af et tal, "
               "samt kundenavne, adresser, varenavne og materialer.",
    )
    return tr.text


# ---- Dansk tal-til-ord (KUN til tale, så cifre ikke læses robotagtigt) ----
_ENER = ["nul", "en", "to", "tre", "fire", "fem", "seks", "syv", "otte", "ni", "ti", "elleve",
         "tolv", "tretten", "fjorten", "femten", "seksten", "sytten", "atten", "nitten"]
_TIERE = {20: "tyve", 30: "tredive", 40: "fyrre", 50: "halvtreds", 60: "tres",
          70: "halvfjerds", 80: "firs", 90: "halvfems"}


def _u100(n):
    if n < 20:
        return _ENER[n]
    t, e = (n // 10) * 10, n % 10
    return _TIERE[t] if e == 0 else _ENER[e] + "og" + _TIERE[t]


def _u1000(n):
    if n < 100:
        return _u100(n)
    h, r = n // 100, n % 100
    s = ("et" if h == 1 else _ENER[h]) + "hundrede"
    return s if r == 0 else s + "og" + _u100(r)


def _tal_til_ord(n):
    if n < 1000:
        return _u1000(n)
    if n < 1000000:
        t, r = n // 1000, n % 1000
        s = ("et" if t == 1 else _u1000(t)) + "tusind"
        return s if r == 0 else s + (" og " if r < 100 else " ") + _u1000(r)
    return " ".join(_ENER[int(c)] for c in str(n))   # store tal (fx telefon) ciffer for ciffer


def _til_tale(text: str) -> str:
    """Gør tekst klar til oplæsning: cifre -> ord, forkortelser udskrevet."""
    t = re.sub(r"\bkr\.?\b", "kroner", text, flags=re.I)
    t = re.sub(r"\bstk\.?\b", "styk", t, flags=re.I)
    t = re.sub(r"\bca\.?\b", "cirka", t, flags=re.I)
    t = re.sub(r"\btlf\.?\b", "telefon", t, flags=re.I)
    t = t.replace("%", " procent")

    def _repl(m):
        num = m.group(0)
        if "," in num or "." in num:
            sep = "," if "," in num else "."
            a, b = num.split(sep, 1)
            heltal = _tal_til_ord(int(a)) if a.isdigit() else a
            brok = " ".join(_ENER[int(c)] for c in b if c.isdigit())
            return f" {heltal} komma {brok} "
        return f" {_tal_til_ord(int(num))} "

    t = re.sub(r"\d+(?:[.,]\d+)?", _repl, t)
    return re.sub(r"\s+", " ", t).strip()


def synthesize_voice(text: str) -> bytes:
    """Lav tale (OGG/Opus) ud fra tekst. Tal/forkortelser laves om til ord, så det
    lyder naturligt på dansk. gpt-4o-*-tts understøtter en tone-instruktion."""
    kwargs = dict(model=OPENAI_TTS_MODEL, voice=OPENAI_TTS_VOICE,
                  input=_til_tale(text), response_format="opus")
    if "gpt-4o" in OPENAI_TTS_MODEL and OPENAI_TTS_INSTRUCTIONS:
        kwargs["instructions"] = OPENAI_TTS_INSTRUCTIONS
    resp = _client.audio.speech.create(**kwargs)
    return resp.content


def send_voice(chat_id, audio: bytes):
    """Send et stemme-svar (voice note) til chatten."""
    requests.post(f"{API}/sendVoice", data={"chat_id": chat_id},
                  files={"voice": ("svar.ogg", audio, "audio/ogg")}, timeout=60)

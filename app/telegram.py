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


def send_message(chat_id, text: str, reply_to=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = True
    requests.get(f"{API}/sendMessage", params=params, timeout=30)


def send_leader(chat_id, text: str):
    """Send en notifikation til lederen OG gem den i samtalehukommelsen,
    saa Aura forstaar opfoelgende svar som 'ja det maa han gerne'."""
    send_message(chat_id, text)
    try:
        from . import db
        db.save_message(chat_id, "assistant", text)
    except Exception:
        pass


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


def _stt_prompt() -> str:
    """Kontekst til talegenkendelsen: fagord + medarbejder-navne, saa navne hoeres rigtigt."""
    dele = ["Dansk talebesked til firmaets assistent om VVS-/el-sager. "
            "Ordet 'sag' efterfølges ofte af et tal. "
            "Fagord: registrer timer, pause, overtid, udkald, færdigmeld, rykker, stregkode, "
            "planlæg, tildel, materialer, vare, dokumentation, reference, montør, bemærkning."]
    navne = []
    try:
        from . import db
        navne += [u.get("navn") for u in db.all_users() if u.get("navn")]
    except Exception:
        pass
    try:
        from . import ordrestyring as os_api
        for u in os_api.users():
            n = (u.get("fullName") or f"{u.get('first_name') or ''} {u.get('last_name') or ''}").strip()
            if n:
                navne.append(n)
    except Exception:
        pass
    if navne:
        unikke = list(dict.fromkeys(navne))[:20]
        dele.append("Navne der ofte nævnes: " + ", ".join(unikke) + ".")
    try:   # kunders navne og adresser -> adresser som "Torvet 6" hoeres rigtigt
        from . import ordrestyring as os_api
        steder = []
        for d in os_api.all_debtors()[:40]:
            n = (d.get("customer_name") or "").strip()
            adr = (d.get("customer_address") or "").strip()
            if n:
                steder.append(n)
            if adr:
                steder.append(adr)
        if steder:
            dele.append("Kunder og adresser: " + ", ".join(dict.fromkeys(steder)) + ".")
    except Exception:
        pass
    return " ".join(dele)[:1800]


def transcribe_voice(file_id: str) -> str:
    audio = io.BytesIO(_download_voice(file_id))
    audio.name = "voice.ogg"
    tr = _client.audio.transcriptions.create(
        model=OPENAI_STT_MODEL, file=audio, language="da",
        prompt=_stt_prompt(),
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
    s = ("et" if h == 1 else _ENER[h]) + " hundrede"
    return s if r == 0 else s + " og " + _u100(r)


def _tal_til_ord(n):
    if n < 1000:
        return _u1000(n)
    if n < 1000000:
        t, r = n // 1000, n % 1000
        s = ("et" if t == 1 else _u1000(t)) + " tusind"
        return s if r == 0 else s + (" og " if r < 100 else " ") + _u1000(r)
    return " ".join(_ENER[int(c)] for c in str(n))   # store tal (fx telefon) ciffer for ciffer


def _til_tale(text: str) -> str:
    """Gør tekst klar til oplæsning: cifre -> ord, forkortelser udskrevet."""
    t = re.sub(r"\bkr\.?\b", "kroner", text, flags=re.I)
    t = re.sub(r"\bstk\.?\b", "styk", t, flags=re.I)
    t = re.sub(r"\bca\.?\b", "cirka", t, flags=re.I)
    t = re.sub(r"\btlf\.?\b", "telefon", t, flags=re.I)
    t = t.replace("%", " procent")

    # Etage-adresser: "2. th." skal LYDE som "anden sal til højre" (aldrig "to t h")
    _ORDINAL = {1: "første", 2: "anden", 3: "tredje", 4: "fjerde", 5: "femte", 6: "sjette",
                7: "syvende", 8: "ottende", 9: "niende", 10: "tiende", 11: "ellevte", 12: "tolvte"}
    _SIDE = {"th": "til højre", "tv": "til venstre", "mf": "midt for"}

    def _etage(m):
        nr, side = int(m.group(1)), m.group(2).lower()
        return f" {_ORDINAL.get(nr, str(nr) + '.')} sal {_SIDE[side]} "

    t = re.sub(r"\b(\d{1,2})\.?\s*(?:sal\s*[, ]?\s*)?(th|tv|mf)\b\.?", _etage, t, flags=re.I)
    t = re.sub(r"\bst\.?\s*(th|tv|mf)\b\.?",
               lambda m: f" stuen {_SIDE[m.group(1).lower()]} ", t, flags=re.I)
    t = re.sub(r"\bkld\.?\b", "kælderen", t, flags=re.I)

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
    _DEFAULT_TONE = ("Du er en dansk kvinde der taler helt naturligt i en telefonsamtale "
                     "med en kollega. Afslappet hverdagsdansk, levende og varieret betoning, "
                     "naturlige mikropauser og små tryk på de vigtige ord — aldrig monotont, "
                     "aldrig oplæst, aldrig som en telefonsvarer. Let smil i stemmen, roligt "
                     "taletempo. Udtal tal og navne tydeligt, men naturligt flydende.")
    if "gpt-4o" in OPENAI_TTS_MODEL:
        kwargs["instructions"] = OPENAI_TTS_INSTRUCTIONS or _DEFAULT_TONE
    resp = _client.audio.speech.create(**kwargs)
    return resp.content


def send_voice(chat_id, audio: bytes, reply_to=None):
    """Send et stemme-svar (voice note) til chatten."""
    data = {"chat_id": chat_id}
    if reply_to:
        data["reply_to_message_id"] = reply_to
        data["allow_sending_without_reply"] = True
    requests.post(f"{API}/sendVoice", data=data,
                  files={"voice": ("svar.ogg", audio, "audio/ogg")}, timeout=60)

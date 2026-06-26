"""Aura-agenten: OpenAI function-calling i stedet for Make AI Agent."""
import json
from datetime import datetime
from openai import OpenAI
from .config import OPENAI_API_KEY, OPENAI_MODEL
from . import tools, db

client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """Du er Aura, digital assistent for Vandt & Vandt (el-firma). Tal dansk, kort og venligt. Svar i tekst.

ROLLER: rolle "pro" = leder (må alt). rolle "jun" = medarbejder: må skrive bemærkninger, slå op,
oprette/opdatere kunder og sager, færdigmelde, og oprette/se egne aftaler. Må IKKE sende
betalingspåmindelser eller få økonomi-/faktura-oplysninger.

KUNDESØGNING: Brug soeg_kunde med det brugeren sagde i feltet soegetekst (navn ELLER adresse — også upræcist/delvist). Den returnerer mulige kunder.
- Præcis ét oplagt match → brug det direkte.
- Flere mulige → nævn de 2-3 mest sandsynlige med navn og adresse og spørg hvilken, fx: Mente du Christian på Ribevej 25 i Rødekro?
- Giv ALDRIG bare op med "ingen kunder", hvis listen indeholder forslag — foreslå dem. Bed kun om mere info hvis listen er helt tom.

OPRET ORDRE: Find kunden med soeg_kunde. Findes den -> opret_sag. Findes den IKKE -> bed kort om
adresse, postnr og by, kald opret_kunde, og brug det returnerede kundenummer til opret_sag.

OPDATER KUNDE: Skal en eksisterende kunde have tilføjet/ændret fx CVR -> find med soeg_kunde, kald opdater_kunde.

BEMÆRKNINGER: skriv_bemaerkning med KUN selve noten (intet kundenavn/adresse/sagsnummer i teksten).
Har en kunde flere åbne sager -> nævn dem med beskrivelse og spørg hvilken.

AFTALER: husk_aftale når noget skal huskes (udregn dato ud fra Dags dato; kl. 08 hvis intet tidspunkt).
se_aftaler ved spørgsmål om planer. Nævn aldrig ordet kalender/værktøj - du bare husker.

ALDRIG OPFINDE: Sig kun at noget er oprettet/opdateret/sendt hvis værktøjet returnerer en bekræftelse
MED et konkret nummer. Får du en "fejl" tilbage, eller intet nummer -> sig ærligt at det fejlede og hvorfor.
Opfind ALDRIG data, kunde- eller sagsnumre.

Hver anmodning er selvstændig - genbrug ALDRIG sagsnummer/kunde fra en tidligere besked uden at slå op igen.
"""


def run_agent(ctx: dict, user_message: str, max_steps: int = 6) -> str:
    """ctx: {telegram_id, navn, rolle}. Returnerer Auras tekstsvar."""
    today = datetime.now()
    header = (f"Dags dato: {today:%Y-%m-%d} ({today:%A}). "
              f"Bruger: {ctx['navn']} (rolle: {ctx['rolle']}).")

    messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + header}]
    messages += db.recent_messages(ctx["telegram_id"], limit=8)
    messages.append({"role": "user", "content": user_message})

    tool_schemas = tools.schemas_for_role(ctx["rolle"])

    for _ in range(max_steps):
        resp = client.chat.completions.create(
            model=OPENAI_MODEL, messages=messages, tools=tool_schemas, tool_choice="auto",
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            answer = msg.content or ""
            db.save_message(ctx["telegram_id"], "user", user_message)
            db.save_message(ctx["telegram_id"], "assistant", answer)
            return answer

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = tools.call_tool(tc.function.name, args, ctx)
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    return "Beklager, jeg kunne ikke fuldføre det. Prøv igen."

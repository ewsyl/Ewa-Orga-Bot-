"""
Ewas Orga-Bot fuer Telegram
- Foto oder Text schicken -> Bot erkennt Termin(e) -> nach Bestaetigung in Google Kalender
- /heute, /morgen, /woche -> Termine anzeigen
- Jeden Morgen automatisch: Tagesuebersicht (Termine + wichtige ungelesene Mails)
"""

import asyncio
import base64
import datetime as dt
import html
import logging
import os
import uuid
from zoneinfo import ZoneInfo

import anthropic
from anthropic import AsyncAnthropic
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Konfiguration (alles ueber Umgebungsvariablen, siehe .env.example)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0") or 0)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
CALENDAR_ID = os.environ.get("CALENDAR_ID", "primary")
TZ_NAME = os.environ.get("TIMEZONE", "Europe/Berlin")
DAILY_TIME = os.environ.get("DAILY_TIME", "07:30")  # HH:MM, leer = aus
TZ = ZoneInfo(TZ_NAME)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
]

WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
DANKE_WORTE = {"danke", "dankeschön", "danke schön", "vielen dank", "danke dir", "merci", "super", "ok", "okay", "top", "👍"}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("orga-bot")

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


def fehlergrund(exc: Exception) -> str:
    """Uebersetzt typische API-Fehler in einen verstaendlichen Hinweis fuer Telegram."""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude-API-Key ungültig → ANTHROPIC_API_KEY in Railway prüfen."
    if isinstance(exc, anthropic.APIStatusError) and "credit balance" in low:
        return "Claude-Guthaben leer → console.anthropic.com → Billing aufladen."
    if isinstance(exc, anthropic.NotFoundError):
        return f"Claude-Modell nicht gefunden → CLAUDE_MODEL prüfen (aktuell: {CLAUDE_MODEL})."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Claude gerade nicht erreichbar → gleich nochmal probieren."
    if isinstance(exc, RefreshError):
        return "Google-Verbindung abgelaufen → neuen Refresh-Token holen (Anleitung Schritt 3d)."
    if isinstance(exc, HttpError):
        if "accessNotConfigured" in text or "has not been used" in text or "is disabled" in text:
            return "Gmail API ist im Google-Projekt nicht aktiviert → Anleitung Schritt 3a."
        if "insufficient" in low or "scope" in low:
            return "Google-Token hat keine Gmail-Berechtigung → Schritt 3d mit beiden Scopes wiederholen."
        return f"Google-Fehler {exc.resp.status}."
    return f"{type(exc).__name__}: {text[:150]}"


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------
def _creds() -> Credentials:
    return Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


def _calendar():
    return build("calendar", "v3", credentials=_creds(), cache_discovery=False)


def _gmail():
    return build("gmail", "v1", credentials=_creds(), cache_discovery=False)


def _parse_time(value: str) -> dt.time:
    value = value.strip().replace(".", ":")
    return dt.datetime.strptime(value, "%H:%M").time()


def build_event_body(ev: dict) -> dict:
    """Wandelt einen erkannten Termin in einen Google-Calendar-Eintrag um."""
    start_d = dt.date.fromisoformat(ev["start_datum"])
    end_d = dt.date.fromisoformat(ev.get("end_datum") or ev["start_datum"])
    if end_d < start_d:
        end_d = start_d

    body = {"summary": ev.get("titel") or "Termin"}
    if ev.get("ort"):
        body["location"] = ev["ort"]
    if ev.get("beschreibung"):
        body["description"] = ev["beschreibung"]

    if ev.get("start_zeit"):
        start = dt.datetime.combine(start_d, _parse_time(ev["start_zeit"]))
        if ev.get("end_zeit"):
            end = dt.datetime.combine(end_d, _parse_time(ev["end_zeit"]))
            if end <= start:  # z. B. bis nach Mitternacht
                end += dt.timedelta(days=1)
        else:
            end = start + dt.timedelta(hours=2)
        body["start"] = {"dateTime": start.isoformat(), "timeZone": TZ_NAME}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": TZ_NAME}
    else:
        # Ganztaegig: Google erwartet das Enddatum exklusiv (+1 Tag)
        body["start"] = {"date": start_d.isoformat()}
        body["end"] = {"date": (end_d + dt.timedelta(days=1)).isoformat()}
    return body


def insert_event(ev: dict) -> str:
    created = _calendar().events().insert(calendarId=CALENDAR_ID, body=build_event_body(ev)).execute()
    return created.get("htmlLink", "")


def list_events(time_min: dt.datetime, time_max: dt.datetime) -> list:
    res = (
        _calendar()
        .events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        )
        .execute()
    )
    return res.get("items", [])


def list_unread_mails(limit: int = 20) -> list:
    svc = _gmail()
    res = (
        svc.users()
        .messages()
        .list(userId="me", q="is:unread category:primary newer_than:1d", maxResults=limit)
        .execute()
    )
    mails = []
    for m in res.get("messages", []):
        msg = (
            svc.users()
            .messages()
            .get(userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "Subject"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        mails.append(
            {
                "von": headers.get("From", ""),
                "betreff": headers.get("Subject", "(kein Betreff)"),
                "vorschau": msg.get("snippet", ""),
            }
        )
    return mails


# ---------------------------------------------------------------------------
# Claude: Termine aus Foto/Text erkennen
# ---------------------------------------------------------------------------
EXTRACT_TOOL = {
    "name": "termine_erfassen",
    "description": "Gibt die erkannten Termine strukturiert zurueck.",
    "input_schema": {
        "type": "object",
        "properties": {
            "termine": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "titel": {"type": "string", "description": "Kurzer, klarer Titel, z. B. 'Flohmarkt Guetersloh'"},
                        "start_datum": {"type": "string", "description": "YYYY-MM-DD"},
                        "start_zeit": {"type": ["string", "null"], "description": "HH:MM oder null wenn ganztaegig"},
                        "end_datum": {"type": ["string", "null"], "description": "YYYY-MM-DD oder null"},
                        "end_zeit": {"type": ["string", "null"], "description": "HH:MM oder null"},
                        "ort": {"type": ["string", "null"], "description": "Adresse oder Ort, so genau wie moeglich"},
                        "beschreibung": {"type": ["string", "null"], "description": "Nuetzliche Zusatzinfos (Eintritt, Hinweise, Website)"},
                    },
                    "required": ["titel", "start_datum"],
                },
            },
            "hinweis": {
                "type": ["string", "null"],
                "description": "Kurzer Hinweis an Ewa, falls etwas unklar war (z. B. Jahr geraten). Sonst null.",
            },
        },
        "required": ["termine"],
    },
}


def _system_prompt() -> str:
    jetzt = dt.datetime.now(TZ)
    return (
        "Du bist Ewas persoenliche Orga-Assistentin. Du liest Fotos (Plakate, Flyer, Screenshots, "
        "Einladungen) oder Texte und erkennst darin Termine fuer ihren Kalender.\n"
        f"Heute ist {WOCHENTAGE[jetzt.weekday()]}, {jetzt:%d.%m.%Y}, {jetzt:%H:%M} Uhr ({TZ_NAME}).\n"
        "Regeln:\n"
        "- Fehlt das Jahr, nimm das naechste zukuenftige Vorkommen.\n"
        "- Relative Angaben ('morgen', 'naechsten Samstag') rechnest du vom heutigen Datum aus.\n"
        "- Mehrtaegige Veranstaltung mit Uhrzeiten pro Tag (z. B. Sa+So jeweils 8-16 Uhr): "
        "lege pro Tag einen eigenen Termin an. Ohne Uhrzeiten: ein ganztaegiger Termin ueber alle Tage.\n"
        "- Keine Uhrzeit erkennbar -> start_zeit null (ganztaegig).\n"
        "- Wenn ein bisheriger Entwurf mitgeschickt wird und Ewa eine Korrektur schreibt, "
        "gib den korrigierten Entwurf vollstaendig zurueck.\n"
        "- Findest du keinen Termin, gib eine leere Liste zurueck und erklaere im hinweis kurz, warum.\n"
        "- Titel, Ort und Beschreibung auf Deutsch, knapp und praktisch."
    )


async def extract_events(text: str | None, image_b64: str | None, media_type: str, draft: list | None) -> dict:
    content = []
    if image_b64:
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}})
    prompt = text or "Bitte trag den Termin von diesem Bild ein."
    if draft:
        import json

        prompt = f"Bisheriger Entwurf:\n{json.dumps(draft, ensure_ascii=False)}\n\nEwas Nachricht:\n{prompt}"
    content.append({"type": "text", "text": prompt})

    resp = await claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=_system_prompt(),
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "termine_erfassen"},
        messages=[{"role": "user", "content": content}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    return {"termine": [], "hinweis": "Ich konnte nichts erkennen."}


# ---------------------------------------------------------------------------
# Formatierung
# ---------------------------------------------------------------------------
def _fmt_date(d: dt.date) -> str:
    return f"{WOCHENTAGE[d.weekday()]}, {d:%d.%m.%Y}"


def format_draft(ev: dict) -> str:
    sd = dt.date.fromisoformat(ev["start_datum"])
    ed = dt.date.fromisoformat(ev.get("end_datum") or ev["start_datum"])
    if ev.get("start_zeit"):
        st = f"{_parse_time(ev['start_zeit']):%H:%M}"
        et = f"{_parse_time(ev['end_zeit']):%H:%M}" if ev.get("end_zeit") else ""
        wann = _fmt_date(sd) + ", " + st + (f"–{et}" if et else "") + " Uhr"
        if ed != sd:
            wann = f"{_fmt_date(sd)} {st} bis {_fmt_date(ed)} {et}".strip()
    else:
        wann = _fmt_date(sd) + (f" bis {_fmt_date(ed)}" if ed != sd else "") + " (ganztägig)"
    lines = [f"📌 <b>{html.escape(ev.get('titel') or 'Termin')}</b>", f"🗓 {wann}"]
    if ev.get("ort"):
        lines.append(f"📍 {html.escape(ev['ort'])}")
    if ev.get("beschreibung"):
        lines.append(f"📝 {html.escape(ev['beschreibung'])}")
    return "\n".join(lines)


def format_calendar_event(item: dict, with_day: bool = False) -> str:
    title = html.escape(item.get("summary", "(ohne Titel)"))
    start = item.get("start", {})
    if "dateTime" in start:
        s = dt.datetime.fromisoformat(start["dateTime"]).astimezone(TZ)
        e = dt.datetime.fromisoformat(item["end"]["dateTime"]).astimezone(TZ)
        when = f"{s:%H:%M}–{e:%H:%M}"
        day = s.date()
    else:
        when = "ganztägig"
        day = dt.date.fromisoformat(start["date"])
    prefix = f"{WOCHENTAGE[day.weekday()]} {day:%d.%m.} · " if with_day else ""
    loc = f" · 📍 {html.escape(item['location'])}" if item.get("location") else ""
    return f"• {prefix}{when} <b>{title}</b>{loc}"


# ---------------------------------------------------------------------------
# Telegram-Handler
# ---------------------------------------------------------------------------
async def _authorized(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if not ALLOWED_USER_ID:
        await update.effective_message.reply_text(
            f"Einrichtung: Deine Telegram-ID ist {uid}.\n"
            "Trag sie in Railway als ALLOWED_USER_ID ein, dann reagiere ich nur noch auf dich."
        )
        return False
    if uid != ALLOWED_USER_ID:
        log.warning("Fremder Zugriff von %s ignoriert", uid)
        return False
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update):
        return
    await update.message.reply_text(
        "Hi Ewa! 👋 So nutzt du mich:\n\n"
        "📸 Foto schicken (Plakat, Flyer, Screenshot) → ich trage den Termin ein\n"
        "✍️ Oder schreib einfach: „Zahnarzt Donnerstag 14 Uhr“\n"
        "🔁 Stimmt was nicht? Schreib die Korrektur, z. B. „erst ab 10 Uhr“\n\n"
        "/heute – Termine heute\n"
        "/morgen – Termine morgen\n"
        "/woche – die nächsten 7 Tage\n"
        "/uebersicht – Tagesübersicht jetzt (Termine + Mails)\n\n"
        f"Die Tagesübersicht kommt automatisch jeden Morgen um {DAILY_TIME} Uhr."
    )


async def _show_range(update: Update, start: dt.datetime, end: dt.datetime, title: str, with_day: bool):
    items = await asyncio.to_thread(list_events, start, end)
    if not items:
        text = f"<b>{title}</b>\nKeine Termine. 🌿"
    else:
        text = f"<b>{title}</b>\n" + "\n".join(format_calendar_event(i, with_day) for i in items)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def _day_bounds(offset: int = 0):
    d = dt.datetime.now(TZ).date() + dt.timedelta(days=offset)
    start = dt.datetime.combine(d, dt.time.min, tzinfo=TZ)
    return start, start + dt.timedelta(days=1)


async def cmd_heute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _authorized(update):
        s, e = _day_bounds(0)
        await _show_range(update, s, e, f"Heute, {_fmt_date(s.date())}", False)


async def cmd_morgen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _authorized(update):
        s, e = _day_bounds(1)
        await _show_range(update, s, e, f"Morgen, {_fmt_date(s.date())}", False)


async def cmd_woche(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _authorized(update):
        s, _ = _day_bounds(0)
        await _show_range(update, s, s + dt.timedelta(days=7), "Die nächsten 7 Tage", True)


async def cmd_uebersicht(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _authorized(update):
        await update.message.reply_text("Moment, ich schau kurz nach … ⏳")
        await send_overview_to(context.bot, update.effective_chat.id)


async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foto, Bild-Datei oder Text -> Termin-Entwurf mit Bestaetigungs-Buttons."""
    if not await _authorized(update):
        return
    msg = update.message
    text = msg.caption or msg.text
    image_b64, media_type = None, "image/jpeg"

    if msg.photo:
        file = await msg.photo[-1].get_file()
        image_b64 = base64.b64encode(bytes(await file.download_as_bytearray())).decode()
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file = await msg.document.get_file()
        image_b64 = base64.b64encode(bytes(await file.download_as_bytearray())).decode()
        media_type = msg.document.mime_type

    # Kurzes Danke & Co. braucht keinen Claude-Aufruf
    if not image_b64 and (text or "").strip().lower().strip("!.😊🙏 ") in DANKE_WORTE:
        await msg.reply_text("Gerne! 😊")
        return

    # Bei reinem Text: letzten offenen Entwurf mitgeben, damit Korrekturen funktionieren
    draft = context.user_data.get("last_draft") if not image_b64 else None

    await context.bot.send_chat_action(msg.chat_id, "typing")
    try:
        result = await extract_events(text, image_b64, media_type, draft)
    except Exception as exc:
        log.exception("Claude-Fehler")
        await msg.reply_text(f"Da ist gerade was schiefgelaufen beim Lesen.\n⚠️ {fehlergrund(exc)}")
        return

    termine = result.get("termine") or []
    hinweis = result.get("hinweis")
    if not termine:
        await msg.reply_text(
            "Ich habe keinen Termin erkannt."
            + (f"\n{hinweis}" if hinweis else "")
            + "\n\nTipp: /heute, /morgen oder /woche zeigen dir deine Termine."
        )
        return

    key = uuid.uuid4().hex[:8]
    context.user_data.setdefault("pending", {})[key] = termine
    context.user_data["last_draft"] = termine
    context.user_data["last_key"] = key

    header = "Soll ich das eintragen?\n\n" if len(termine) == 1 else f"Ich habe {len(termine)} Termine gefunden:\n\n"
    body = "\n\n".join(format_draft(t) for t in termine)
    if hinweis:
        body += f"\n\nℹ️ {html.escape(hinweis)}"
    buttons = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Eintragen", callback_data=f"ok:{key}"),
            InlineKeyboardButton("❌ Verwerfen", callback_data=f"no:{key}"),
        ]]
    )
    await msg.reply_text(
        header + body + "\n\n<i>Stimmt was nicht? Schreib mir einfach die Korrektur.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=buttons,
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not ALLOWED_USER_ID or query.from_user.id != ALLOWED_USER_ID:
        await query.answer()
        return
    await query.answer()
    action, key = query.data.split(":", 1)
    termine = context.user_data.get("pending", {}).pop(key, None)
    if context.user_data.get("last_key") == key:
        context.user_data.pop("last_draft", None)

    if termine is None:
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("Dieser Entwurf ist nicht mehr aktiv (evtl. schon erledigt oder korrigiert).")
        return

    if action == "no":
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("Okay, verworfen. 👍")
        return

    ergebnisse = []
    for ev in termine:
        try:
            link = await asyncio.to_thread(insert_event, ev)
            titel = html.escape(ev.get("titel") or "Termin")
            ergebnisse.append(f"✅ <a href=\"{link}\">{titel}</a>" if link else f"✅ {titel}")
        except Exception:
            log.exception("Kalender-Fehler")
            ergebnisse.append(f"⚠️ {html.escape(ev.get('titel') or 'Termin')} konnte nicht eingetragen werden")
    await query.edit_message_reply_markup(None)
    await query.message.reply_text(
        "Im Kalender:\n" + "\n".join(ergebnisse), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


# ---------------------------------------------------------------------------
# Tagesuebersicht
# ---------------------------------------------------------------------------
async def summarize_mails(mails: list) -> str:
    if not mails:
        return "Keine neuen ungelesenen Mails im Hauptpostfach. 🎉"
    liste = "\n".join(f"- Von: {m['von']} | Betreff: {m['betreff']} | {m['vorschau']}" for m in mails)
    resp = await claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=600,
        system=(
            "Du fasst Ewas ungelesene Mails fuer ihre Morgenuebersicht zusammen. Deutsch, knapp. "
            "Nenne hoechstens 5 wirklich wichtige Mails (Kunden, Team, Termine, Zahlungen, Fristen) "
            "als Zeilen im Format '• Absender – worum es geht (was zu tun ist)'. "
            "Newsletter, Werbung und Benachrichtigungen fasst du in einer Schlusszeile zusammen ('+ X weitere unwichtige'). "
            "Kein Markdown, keine Ueberschrift."
        ),
        messages=[{"role": "user", "content": liste}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def send_overview_to(bot, chat_id: int):
    s, e = _day_bounds(0)
    teile = [f"☀️ <b>Guten Morgen, Ewa!</b>\n{_fmt_date(s.date())}"]
    try:
        items = await asyncio.to_thread(list_events, s, e)
        termine = "\n".join(format_calendar_event(i) for i in items) if items else "Heute keine Termine. 🌿"
    except Exception:
        log.exception("Kalender-Fehler")
        termine = "⚠️ Kalender konnte nicht geladen werden."
    teile.append(f"<b>📅 Termine heute</b>\n{termine}")

    try:
        mails = await asyncio.to_thread(list_unread_mails)
    except Exception as exc:
        log.exception("Gmail-Fehler")
        mails = None
        mail_text = f"⚠️ Mails konnten nicht geladen werden.\n{html.escape(fehlergrund(exc))}"
    if mails is not None:
        try:
            mail_text = html.escape(await summarize_mails(mails))
        except Exception as exc:
            # Claude streikt -> wenigstens die Betreffzeilen zeigen
            log.exception("Claude-Fehler (Mail-Zusammenfassung)")
            liste = "\n".join(f"• {m['von'].split('<')[0].strip()} – {m['betreff']}" for m in mails[:8])
            mail_text = html.escape(liste) + f"\n<i>(Zusammenfassung nicht möglich: {html.escape(fehlergrund(exc))})</i>"
    teile.append(f"<b>📬 Postfach</b>\n{mail_text}")

    await bot.send_message(chat_id, "\n\n".join(teile), parse_mode=ParseMode.HTML)


async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID:
        await send_overview_to(context.bot, ALLOWED_USER_ID)


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
def main():
    missing = [
        n
        for n, v in {
            "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
            "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
            "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
            "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
            "GOOGLE_REFRESH_TOKEN": GOOGLE_REFRESH_TOKEN,
        }.items()
        if not v
    ]
    if missing:
        raise SystemExit("Fehlende Variablen: " + ", ".join(missing))

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("hilfe", cmd_start))
    app.add_handler(CommandHandler("heute", cmd_heute))
    app.add_handler(CommandHandler("morgen", cmd_morgen))
    app.add_handler(CommandHandler("woche", cmd_woche))
    app.add_handler(CommandHandler("uebersicht", cmd_uebersicht))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.IMAGE | (filters.TEXT & ~filters.COMMAND),
            handle_input,
        )
    )

    if DAILY_TIME:
        hh, mm = (int(x) for x in DAILY_TIME.split(":"))
        app.job_queue.run_daily(daily_job, time=dt.time(hh, mm, tzinfo=TZ), name="tagesuebersicht")
        log.info("Tagesuebersicht taeglich um %s (%s)", DAILY_TIME, TZ_NAME)

    log.info("Bot laeuft.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

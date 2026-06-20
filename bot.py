import os
import re
import logging
import asyncio
import requests
from datetime import datetime
import pytz
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

import db
import webhook_server
import report

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
AMO_TOKEN = os.environ.get("AMO_TOKEN")
AMO_DOMAIN = os.environ.get("AMO_DOMAIN", "juz40online")
GROUP_ID = int(os.environ.get("GROUP_ID", "-1002234853365"))
PORT = int(os.environ.get("PORT", "8080"))

AMO_BASE = f"https://{AMO_DOMAIN}.amocrm.ru/api/v4"
HEADERS = {
    "Authorization": f"Bearer {AMO_TOKEN}",
    "Content-Type": "application/json"
}

ALMATY_TZ = pytz.timezone("Asia/Almaty")


# =========================================================
# ТЕЛЕФОН ИЗВЛЕЧЕНИЕ
# =========================================================

def normalize_phone(raw: str) -> str:
    """Кез-келген форматтан тек цифрларды алып, 7XXXXXXXXXX форматқа келтіреді."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits


def extract_phones(text: str) -> list[str]:
    """Мәтіннен барлық телефон нөмірлерін табады."""
    pattern = r'[\+\s\-\(\)]*(7|8)[\s\-\(\)]*\d[\s\-\(\)]*\d[\s\-\(\)]*\d[\s\-\(\)]*\d[\s\-\(\)]*\d[\s\-\(\)]*\d[\s\-\(\)]*\d[\s\-\(\)]*\d[\s\-\(\)]*\d[\s\-\(\)]*\d'
    phones = []
    for m in re.finditer(pattern, text):
        normalized = normalize_phone(m.group())
        if len(normalized) == 11 and normalized.startswith("7"):
            phones.append(normalized)
    return list(set(phones))


# =========================================================
# AMOCRM ФУНКЦИЯЛАРЫ
# =========================================================

def search_contact_by_phone(phone: str) -> dict | None:
    url = f"{AMO_BASE}/contacts"
    params = {"query": phone, "with": "leads"}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            contacts = data.get("_embedded", {}).get("contacts", [])
            if contacts:
                return contacts[0]
    except Exception as e:
        logger.error(f"Contact search error: {e}")
    return None


def get_lead(lead_id: int) -> dict | None:
    url = f"{AMO_BASE}/leads/{lead_id}?with=contacts"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"Lead fetch error: {e}")
    return None


def get_user(user_id: int) -> str:
    url = f"{AMO_BASE}/users/{user_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("name", "Белгісіз")
    except Exception as e:
        logger.error(f"User fetch error: {e}")
    return "Белгісіз"


def update_contact_responsible(contact_id: int, responsible_user_id: int) -> bool:
    url = f"{AMO_BASE}/contacts/{contact_id}"
    payload = {"responsible_user_id": responsible_user_id}
    try:
        resp = requests.patch(url, headers=HEADERS, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Contact update error: {e}")
        return False


def build_response(phone: str) -> str:
    """Телефон нөмірі бойынша толық жауап жасайды."""
    contact = search_contact_by_phone(phone)

    if not contact:
        return f"❌ *{phone}* — amoCRM-де табылмады"

    contact_id = contact["id"]
    contact_name = contact.get("name", "Аты жоқ")
    contact_url = f"https://{AMO_DOMAIN}.amocrm.ru/contacts/detail/{contact_id}"

    leads = contact.get("_embedded", {}).get("leads", [])

    if not leads:
        return (
            f"✅ *Контакт табылды:* [{contact_name}]({contact_url})\n"
            f"📱 Нөмір: `{phone}`\n"
            f"📋 Сделка: жоқ"
        )

    lines = [
        f"✅ *Контакт:* [{contact_name}]({contact_url})",
        f"📱 `{phone}`",
        ""
    ]

    for lead_ref in leads[:3]:
        lead = get_lead(lead_ref["id"])
        if not lead:
            continue

        lead_id = lead["id"]
        lead_name = lead.get("name", "Сделка")
        responsible_id = lead.get("responsible_user_id")
        manager_name = get_user(responsible_id) if responsible_id else "Белгісіз"
        lead_url = f"https://{AMO_DOMAIN}.amocrm.ru/leads/detail/{lead_id}"

        lines.append(f"🔗 *Сделка:* [{lead_name}]({lead_url})")
        lines.append(f"👤 *Менеджер:* {manager_name}")
        lines.append("")

    return "\n".join(lines).strip()


def extract_lead_id(text: str) -> int | None:
    match = re.search(r'amocrm\.ru/leads/detail/(\d+)', text)
    if match:
        return int(match.group(1))
    return None


def sync_contact_responsible(lead_id: int) -> str:
    """Сделканың ответственныйын сол сделкадағы контактқа көшіреді."""
    lead = get_lead(lead_id)
    if not lead:
        return f"❌ Сделка табылмады (ID: {lead_id})"

    lead_name = lead.get("name", "Сделка")
    lead_responsible_id = lead.get("responsible_user_id")
    if not lead_responsible_id:
        return "❌ Сделкада ответственный анықталмаған"

    manager_name = get_user(lead_responsible_id)

    contacts = lead.get("_embedded", {}).get("contacts", [])
    if not contacts:
        return f"❌ Сделкада ({lead_name}) контакт табылмады"

    contact_id = contacts[0]["id"]

    contact_url_api = f"{AMO_BASE}/contacts/{contact_id}"
    try:
        resp = requests.get(contact_url_api, headers=HEADERS, timeout=10)
        contact_data = resp.json() if resp.status_code == 200 else {}
    except Exception:
        contact_data = {}

    contact_name = contact_data.get("name", "Аты жоқ")
    current_responsible_id = contact_data.get("responsible_user_id")

    if current_responsible_id == lead_responsible_id:
        return (
            f"ℹ️ *{contact_name}* контактының ответственныйы\n"
            f"бұрыннан *{manager_name}* болып тұрған, өзгеріс жасалмады"
        )

    success = update_contact_responsible(contact_id, lead_responsible_id)
    contact_url = f"https://{AMO_DOMAIN}.amocrm.ru/contacts/detail/{contact_id}"

    if success:
        return (
            f"✅ Ауыстырылды!\n"
            f"👤 *Контакт:* [{contact_name}]({contact_url})\n"
            f"📋 *Сделка:* {lead_name}\n"
            f"🔄 Жаңа ответственный: *{manager_name}*"
        )
    else:
        return "❌ Контактты ауыстыру кезінде қате шықты"


# =========================================================
# TELEGRAM HANDLERS
# =========================================================

def is_bot_mentioned(msg, bot_username: str) -> bool:
    """Хабарламада бот mention болған ба тексереді."""
    if not msg.entities:
        return False
    for entity in msg.entities:
        if entity.type == "mention":
            mention_text = msg.text[entity.offset: entity.offset + entity.length]
            if mention_text.lower() == f"@{bot_username.lower()}":
                return True
    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Группадағы хабарламаларды өңдейді."""
    msg = update.message
    if not msg or not msg.text:
        return

    if msg.chat_id != GROUP_ID:
        return

    # 1) Lead ссылкасы бар ма? Mention керек емес
    lead_id = extract_lead_id(msg.text)
    if lead_id:
        response = sync_contact_responsible(lead_id)
        await msg.reply_text(response, parse_mode="Markdown", disable_web_page_preview=True)
        return

    # 2) Телефон нөмірі — тек mention болғанда
    bot_username = context.bot.username
    if not is_bot_mentioned(msg, bot_username):
        return

    phones = extract_phones(msg.text)
    if not phones:
        await msg.reply_text("📵 Нөмір табылмады. Мысалы: @bot +77001234567", parse_mode="Markdown")
        return

    for phone in phones:
        response = build_response(phone)
        await msg.reply_text(response, parse_mode="Markdown", disable_web_page_preview=True)


# =========================================================
# КҰНДЕЛІКТІ ЕСЕП (СРЕЗ) ЖІБЕРУ
# =========================================================

async def send_scheduled_report(app: Application):
    """Жоспарланған уақытта группаға есеп жібереді."""
    try:
        report_text = report.build_report()
        await app.bot.send_message(chat_id=GROUP_ID, text=report_text)
        logger.info("Scheduled report sent successfully")
    except Exception as e:
        logger.error(f"send_scheduled_report error: {e}")
        try:
            await app.bot.send_message(chat_id=GROUP_ID, text=f"⚠️ Есеп жіберу кезінде қате: {e}")
        except Exception:
            pass


def setup_scheduler(app: Application) -> AsyncIOScheduler:
    """15:55, 18:25, 19:45 (Almaty уақыты) уақыттарына cron орнатады."""
    scheduler = AsyncIOScheduler(timezone=ALMATY_TZ)

    times = [(15, 55), (18, 25), (19, 45)]
    for hour, minute in times:
        scheduler.add_job(
            send_scheduled_report,
            "cron",
            hour=hour,
            minute=minute,
            args=[app],
            id=f"report_{hour}_{minute}",
        )
    scheduler.start()
    logger.info(f"Scheduler started with jobs at: {times}")
    return scheduler


# =========================================================
# MAIN — TELEGRAM BOT + WEBHOOK SERVER БІРГЕ ІСКЕ ҚОСУ
# =========================================================

async def run_webhook_server():
    """aiohttp webhook серверін бөлек портта жұмыс істетеді."""
    app = webhook_server.create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Webhook server listening on port {PORT}")


async def main():
    db.init_db()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    setup_scheduler(application)
    await run_webhook_server()

    logger.info("Бот және webhook сервер іске қосылды...")

    # Мәңгі жұмыс істеу үшін
    stop_event = asyncio.Event()
    await stop_event.wait()


if __name__ == "__main__":
    asyncio.run(main())

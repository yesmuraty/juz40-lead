import os
import re
import logging
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
AMO_TOKEN = os.environ.get("AMO_TOKEN")
AMO_DOMAIN = os.environ.get("AMO_DOMAIN", "juz40online")
GROUP_ID = int(os.environ.get("GROUP_ID", "-1004486622037"))

AMO_BASE = f"https://{AMO_DOMAIN}.amocrm.ru/api/v4"
HEADERS = {
    "Authorization": f"Bearer {AMO_TOKEN}",
    "Content-Type": "application/json"
}


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
    raw_matches = re.findall(pattern, text)
    # Толық нөмірді алу үшін finditer қолданамыз
    phones = []
    for m in re.finditer(pattern, text):
        normalized = normalize_phone(m.group())
        if len(normalized) == 11 and normalized.startswith("7"):
            phones.append(normalized)
    return list(set(phones))


def search_contact_by_phone(phone: str) -> dict | None:
    """amoCRM-де телефон бойынша контакт іздейді."""
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
    """Сделка мәліметтерін алады."""
    url = f"{AMO_BASE}/leads/{lead_id}?with=contacts"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"Lead fetch error: {e}")
    return None


def get_user(user_id: int) -> str:
    """Менеджер атын алады."""
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
    """Контакттың ответственный менеджерін ауыстырады."""
    url = f"{AMO_BASE}/contacts/{contact_id}"
    payload = {"responsible_user_id": responsible_user_id}
    try:
        resp = requests.patch(url, headers=HEADERS, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Contact update error: {e}")
        return False


def build_response(phone: str) -> str:
    """Нөмір бойынша толық жауап жасайды."""
    contact = search_contact_by_phone(phone)

    if not contact:
        return f"❌ *{phone}* — amoCRM-де табылмады"

    contact_id = contact["id"]
    contact_name = contact.get("name", "Аты жоқ")
    contact_url = f"https://{AMO_DOMAIN}.amocrm.ru/contacts/detail/{contact_id}"

    # Сделкаларды алу
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

    for lead_ref in leads[:3]:  # максимум 3 сделка
        lead = get_lead(lead_ref["id"])
        if not lead:
            continue

        lead_id = lead["id"]
        lead_name = lead.get("name", "Сделка")
        lead_status = lead.get("status_id", "")
        responsible_id = lead.get("responsible_user_id")
        manager_name = get_user(responsible_id) if responsible_id else "Белгісіз"
        lead_url = f"https://{AMO_DOMAIN}.amocrm.ru/leads/detail/{lead_id}"

        lines.append(f"🔗 *Сделка:* [{lead_name}]({lead_url})")
        lines.append(f"👤 *Менеджер:* {manager_name}")
        lines.append("")

    return "\n".join(lines).strip()


def extract_lead_id(text: str) -> int | None:
    """Мәтіннен amoCRM lead ссылкасының ID-сін табады."""
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

    # Контакттың қазіргі мәліметін аламыз (аты үшін)
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

    # Тек белгіленген группадан
    if msg.chat_id != GROUP_ID:
        return

    # 1) Lead ссылкасы бар ма? Mention керек емес, барлық менеджер қолдана алады
    lead_id = extract_lead_id(msg.text)
    if lead_id:
        response = sync_contact_responsible(lead_id)
        await msg.reply_text(response, parse_mode="Markdown", disable_web_page_preview=True)
        return

    # 2) Телефон нөмірін табу — тек бот mention болғанда жұмыс істейді
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


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот іске қосылды...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

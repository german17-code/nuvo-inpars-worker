"""
NuVo Inpars Worker — сервис автоматического сбора объявлений с Inpars API.

Каждые N минут опрашивает Inpars, фильтрует по нашим параметрам,
дедуплицирует через listing_id, отправляет:
  - в общий Telegram-чат с кнопками действий
  - в Google Sheets (тот же документ, лист "Объекты")
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

# ---------- Часовой пояс ----------
MSK = timezone(timedelta(hours=3))

def now_msk_str() -> str:
    """Возвращает текущее московское время в формате 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


# ---------- Конфигурация ----------
TELEGRAM_TOKEN          = os.environ["TELEGRAM_TOKEN"]
INPARS_TOKEN            = os.environ["INPARS_TOKEN"]
OBJECTS_CHAT_ID         = int(os.environ["OBJECTS_CHAT_ID"])
SHEETS_OBJECTS_WEBHOOK  = os.environ.get("SHEETS_OBJECTS_WEBHOOK", "").strip()

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
TEST_INITIAL_DUMP = int(os.environ.get("TEST_INITIAL_DUMP", "0"))

# ---------- Фильтры ----------
INPARS_FILTERS = {
    "regionId":   77,
    "cityId":     1,
    "typeAd":     1,
    "sectionId":  6,
    "sellerType": 1,
    "withPhoto":  1,
    "costMin":    130000,
    "sourceId":   "1,2,13,22",
    "limit":      100,
    "sortBy":     "id_desc",
}

ALLOWED_CATEGORY_IDS: set[int] = set()

SOURCE_LABELS = {
    1:  "Avito",
    2:  "Cian",
    13: "Я.Недвижимость",
    22: "DomClick",
}

# ---------- Логирование ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("inpars-worker")


# ---------- Inpars API ----------
INPARS_BASE = "https://inpars.ru/api/v2"

async def inpars_request(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    p = dict(params or {})
    p["access-token"] = INPARS_TOKEN
    r = await client.get(f"{INPARS_BASE}{path}", params=p, timeout=30.0)
    r.raise_for_status()
    return r.json()


async def load_category_ids(client: httpx.AsyncClient) -> set[int]:
    data = await inpars_request(client, "/estate/category", {"sectionId": 6})
    ids = set()
    for c in data.get("data", []):
        title = (c.get("title") or "").lower()
        if "квартир" in title or "апартамент" in title or "студи" in title:
            ids.add(c["id"])
            logger.info(f"Категория [{c['id']}] {c['title']} — включена")
    if not ids:
        logger.warning("Не нашли подходящих категорий")
    return ids


async def fetch_new_listings(client: httpx.AsyncClient, last_seen_id: int) -> list[dict]:
    params = dict(INPARS_FILTERS)
    if last_seen_id:
        params["lastId"] = last_seen_id
        params["sortBy"] = "id_asc"
    params["expand"] = "rentTime,isApartments,rooms,district,metro"

    data = await inpars_request(client, "/estate", params)
    listings = data.get("data", [])
    logger.info(f"Inpars вернул {len(listings)} объявлений (после lastId={last_seen_id})")
    return listings


def passes_filters(listing: dict) -> bool:
    if listing.get("rentTime") == 2:
        return False
    if ALLOWED_CATEGORY_IDS and listing.get("categoryId") not in ALLOWED_CATEGORY_IDS:
        return False
    if listing.get("agent") != 0:
        return False
    return True


# ---------- Сообщение в Telegram ----------
def format_listing_message(listing: dict) -> str:
    cost = listing.get("cost", 0)
    cost_str = f"{cost:,}".replace(",", " ") + " ₽/мес" if cost else "цена не указана"

    rooms = listing.get("rooms")
    is_apt = listing.get("isApartments")
    title = listing.get("title", "")

    if is_apt:
        kind = f"{rooms}-к апартаменты" if rooms else "Апартаменты"
    elif rooms:
        kind = f"{rooms}-к квартира" if rooms > 0 else "Студия"
    else:
        kind = title or "Жильё"

    sq = listing.get("sq", 0)
    floor = listing.get("floor", 0)
    floors = listing.get("floors", 0)
    floor_str = f"{floor}/{floors} эт." if floor and floors else ""

    address = listing.get("address", "—")
    metro = listing.get("metro", "")
    district = listing.get("district", "")
    location = ", ".join(filter(None, [district, metro]))

    source = SOURCE_LABELS.get(listing.get("sourceId"), listing.get("source", "?"))
    url = listing.get("url", "")

    name = listing.get("name") or "Собственник"
    phones = listing.get("phones") or []
    phone_str = ""
    if phones:
        p = str(phones[0])
        if p.startswith("7") and len(p) == 11:
            phone_str = f"+7 ({p[1:4]}) {p[4:7]}-{p[7:9]}-{p[9:11]}"
        else:
            phone_str = p

    parts = [
        f"💎 *{kind}*  ·  *{cost_str}*",
        f"📐 {sq} м²" + (f"  ·  {floor_str}" if floor_str else ""),
        f"📍 {address}",
    ]
    if location:
        parts.append(f"🚇 {location}")
    parts.append("")
    parts.append(f"👤 {name}" + (f"  ·  `{phone_str}`" if phone_str else ""))
    parts.append(f"🌐 [{source}]({url})")

    return "\n".join(p for p in parts if p is not None)


def make_action_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Взять в работу",  callback_data=f"lead:take:{listing_id}"),
            InlineKeyboardButton("⏰ Перезвонить",     callback_data=f"lead:later:{listing_id}"),
        ],
        [
            InlineKeyboardButton("❌ Не подходит",     callback_data=f"lead:skip:{listing_id}"),
        ],
    ])


# ---------- Google Sheets ----------
async def send_to_sheets(client: httpx.AsyncClient, payload: dict) -> None:
    if not SHEETS_OBJECTS_WEBHOOK:
        return
    try:
        r = await client.post(
            SHEETS_OBJECTS_WEBHOOK, json=payload, timeout=15.0,
            follow_redirects=True,
        )
        if r.status_code != 200:
            logger.warning(f"Sheets вернул {r.status_code}: {r.text[:200]}")
    except Exception:
        logger.exception("Не удалось записать объект в Google Sheets")


async def update_sheet_status(client: httpx.AsyncClient, listing_id: int, status: str, manager: str) -> None:
    if not SHEETS_OBJECTS_WEBHOOK:
        return
    payload = {
        "action":     "update_status",
        "listing_id": listing_id,
        "status":     status,
        "manager":    manager,
        "updated":    now_msk_str(),
    }
    try:
        await client.post(
            SHEETS_OBJECTS_WEBHOOK, json=payload, timeout=15.0,
            follow_redirects=True,
        )
    except Exception:
        logger.exception("Не удалось обновить статус в Sheets")


# ---------- Подготовка данных для Sheets ----------
def build_sheets_payload(listing: dict) -> dict:
    phones = listing.get("phones") or []
    phone = str(phones[0]) if phones else ""

    rent_terms = listing.get("rentTerms") or {}
    is_apt_str = "Апартаменты" if listing.get("isApartments") else "Квартира"

    return {
        "action":      "add_listing",
        "listing_id":  listing.get("id"),
        "timestamp":   now_msk_str(),
        "status":      "новый",
        "manager":     "",
        "source":      SOURCE_LABELS.get(listing.get("sourceId"), listing.get("source", "")),
        "url":         listing.get("url", ""),
        "is_apt":      is_apt_str,
        "rooms":       listing.get("rooms", ""),
        "sq":          listing.get("sq", ""),
        "cost":        listing.get("cost", ""),
        "floor":       listing.get("floor", ""),
        "floors":      listing.get("floors", ""),
        "address":     listing.get("address", ""),
        "district":    listing.get("district", ""),
        "metro":       listing.get("metro", ""),
        "name":        listing.get("name", ""),
        "phone":       phone,
        "commission":  rent_terms.get("commission", ""),
        "deposit":     rent_terms.get("deposit", ""),
        "comment":     "",
    }


# ---------- Дедупликация ----------
last_seen_id_state: dict[str, int] = {"value": 0}


async def get_initial_last_id(client: httpx.AsyncClient) -> int:
    params = dict(INPARS_FILTERS)
    params["sortBy"] = "id_desc"
    params["limit"] = 1
    data = await inpars_request(client, "/estate", params)
    listings = data.get("data", [])
    if listings:
        latest_id = listings[0]["id"]
        logger.info(f"Стартовый last_seen_id = {latest_id}")
        return latest_id
    return 0


async def fetch_recent_for_test(client: httpx.AsyncClient, n: int) -> list[dict]:
    params = dict(INPARS_FILTERS)
    params["sortBy"] = "id_desc"
    params["limit"] = max(n * 5, 50)
    params["expand"] = "rentTime,isApartments,rooms,district,metro"
    data = await inpars_request(client, "/estate", params)
    listings = data.get("data", [])
    passing = [l for l in listings if passes_filters(l)]
    return passing[:n]


# ---------- Главный цикл ----------
async def polling_loop(application: Application) -> None:
    async with httpx.AsyncClient() as client:
        global ALLOWED_CATEGORY_IDS
        try:
            ALLOWED_CATEGORY_IDS = await load_category_ids(client)
        except Exception:
            logger.exception("Не удалось загрузить категории")
            ALLOWED_CATEGORY_IDS = set()

        try:
            last_seen_id_state["value"] = await get_initial_last_id(client)
        except Exception:
            logger.exception("Не удалось получить стартовый last_id")

        # ТЕСТОВЫЙ РЕЖИМ
        if TEST_INITIAL_DUMP > 0:
            logger.info(f"🧪 ТЕСТОВЫЙ ДАМП: запрашиваю {TEST_INITIAL_DUMP} объявлений")
            try:
                test_listings = await fetch_recent_for_test(client, TEST_INITIAL_DUMP)
                logger.info(f"🧪 Получено {len(test_listings)} объявлений после фильтрации")

                try:
                    await application.bot.send_message(
                        chat_id=OBJECTS_CHAT_ID,
                        text=(
                            f"🧪 *Тестовый запуск*\n"
                            f"Сейчас придёт {len(test_listings)} последних объявлений из Inpars, "
                            f"чтобы убедиться, что всё работает. Дальше — только новые в реальном времени."
                        ),
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("Не удалось отправить пометку")

                for listing in test_listings:
                    listing_id = listing.get("id", 0)
                    try:
                        text = format_listing_message(listing)
                        await application.bot.send_message(
                            chat_id=OBJECTS_CHAT_ID,
                            text=text,
                            parse_mode="Markdown",
                            disable_web_page_preview=False,
                            reply_markup=make_action_keyboard(listing_id),
                        )
                    except Exception:
                        logger.exception(f"Не удалось отправить тестовый объект {listing_id}")

                    sheets_payload = build_sheets_payload(listing)
                    await send_to_sheets(client, sheets_payload)
                    await asyncio.sleep(0.5)

                logger.info("🧪 Тестовый дамп завершён.")
            except Exception:
                logger.exception("Ошибка в тестовом дампе")

        logger.info(f"Старт цикла опроса с интервалом {POLL_INTERVAL_SECONDS} с")

        while True:
            try:
                listings = await fetch_new_listings(client, last_seen_id_state["value"])
                listings.sort(key=lambda x: x.get("id", 0))

                new_max_id = last_seen_id_state["value"]
                shipped = 0

                for listing in listings:
                    listing_id = listing.get("id", 0)
                    if listing_id <= last_seen_id_state["value"]:
                        continue
                    new_max_id = max(new_max_id, listing_id)

                    if not passes_filters(listing):
                        continue

                    try:
                        text = format_listing_message(listing)
                        await application.bot.send_message(
                            chat_id=OBJECTS_CHAT_ID,
                            text=text,
                            parse_mode="Markdown",
                            disable_web_page_preview=False,
                            reply_markup=make_action_keyboard(listing_id),
                        )
                        shipped += 1
                    except Exception:
                        logger.exception(f"Не удалось отправить объект {listing_id}")

                    sheets_payload = build_sheets_payload(listing)
                    await send_to_sheets(client, sheets_payload)
                    await asyncio.sleep(0.5)

                last_seen_id_state["value"] = new_max_id
                logger.info(f"Цикл завершён. Отправлено: {shipped}. last_seen_id={new_max_id}")

            except Exception:
                logger.exception("Ошибка в цикле опроса")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ---------- Кнопки в Telegram ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, action, listing_id_str = query.data.split(":", 2)
        listing_id = int(listing_id_str)
    except Exception:
        await query.answer("Некорректные данные кнопки", show_alert=True)
        return

    user = update.effective_user
    manager_name = user.first_name or "—"
    if user.username:
        manager_name = f"{manager_name} (@{user.username})"

    actions = {
        "take":  ("в работе",      f"✅ Взял в работу: {manager_name}"),
        "later": ("перезвонить",   f"⏰ Перезвонить позже — {manager_name}"),
        "skip":  ("не подходит",   f"❌ Не подходит — {manager_name}"),
    }
    status, footer = actions.get(action, ("неизвестно", "?"))

    new_text = (query.message.text_markdown or query.message.text or "") + f"\n\n— — —\n*{footer}*"
    try:
        await query.edit_message_text(
            text=new_text,
            parse_mode="Markdown",
            disable_web_page_preview=False,
            reply_markup=None,
        )
    except Exception:
        logger.exception("Не удалось отредактировать сообщение")

    async with httpx.AsyncClient() as client:
        await update_sheet_status(client, listing_id, status, manager_name)


# ---------- Точка входа ----------
async def post_init(application: Application) -> None:
    asyncio.create_task(polling_loop(application))
    logger.info("Inpars worker готов к работе")


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^lead:"))
    logger.info("Запускаю Inpars worker...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


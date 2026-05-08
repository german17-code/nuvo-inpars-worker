"""
NuVo Inpars Worker — сервис автоматического сбора объявлений с Inpars API.

Каждые N минут опрашивает Inpars, фильтрует по нашим параметрам,
дедуплицирует через parseId+sourceId, отправляет:
  - в общий Telegram-чат с кнопками действий
  - в Google Sheets (тот же документ, лист "Объекты")

Сервис независим от bot.py (бота-квалификатора), но пишет в ту же таблицу.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

# ---------- Конфигурация ----------
TELEGRAM_TOKEN          = os.environ["TELEGRAM_TOKEN"]
INPARS_TOKEN            = os.environ["INPARS_TOKEN"]
OBJECTS_CHAT_ID         = int(os.environ["OBJECTS_CHAT_ID"])
SHEETS_OBJECTS_WEBHOOK  = os.environ.get("SHEETS_OBJECTS_WEBHOOK", "").strip()

# Как часто опрашиваем API (в секундах)
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))  # 5 минут

# ---------- Фильтры (ваши настройки) ----------
INPARS_FILTERS = {
    "regionId":   77,           # Москва
    "cityId":     1,            # Москва
    "typeAd":     1,            # сдам (собственник сдаёт)
    "sectionId":  6,            # жилая недвижимость (аренда)
    "sellerType": 1,            # только собственник
    "withPhoto":  1,            # только с фото
    "costMin":    150000,       # от 150 000 ₽
    # костMax не задан — без верхней границы
    "sourceId":   "1,2,13,22",  # avito, cian, yandex.realty, domclick
    "limit":      100,          # за один запрос — до 100 объявлений
    "sortBy":     "id_desc",    # самые свежие первыми
}

# Категории "квартиры + апартаменты" — будут подставлены при старте после справочника
ALLOWED_CATEGORY_IDS: set[int] = set()

# Источники для красивого отображения
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

# ---------- Работа с Inpars API ----------
INPARS_BASE = "https://inpars.ru/api/v2"

async def inpars_request(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    """Запрос к Inpars API. Токен передаём в access-token."""
    p = dict(params or {})
    p["access-token"] = INPARS_TOKEN
    r = await client.get(f"{INPARS_BASE}{path}", params=p, timeout=30.0)
    r.raise_for_status()
    return r.json()


async def load_category_ids(client: httpx.AsyncClient) -> set[int]:
    """Загружаем справочник категорий и находим ID для 'Квартира' и 'Апартаменты'.

    Inpars в `category` для аренды (typeId=1) возвращает категории квартир/комнат/домов.
    Нам нужны те, чьё название содержит 'квартир' или 'апартамент'.
    """
    data = await inpars_request(client, "/estate/category", {"sectionId": 6})
    ids = set()
    for c in data.get("data", []):
        title = (c.get("title") or "").lower()
        # Берём всё, что про квартиры (1-к, 2-к, 3-к, 4+, студия)
        # и про апартаменты. Не берём комнаты, дома, гаражи.
        if "квартир" in title or "апартамент" in title or "студи" in title:
            ids.add(c["id"])
            logger.info(f"Категория [{c['id']}] {c['title']} — включена")
    if not ids:
        logger.warning("Не нашли подходящих категорий — фильтр по категориям отключён")
    return ids


async def fetch_new_listings(client: httpx.AsyncClient, last_seen_id: int) -> list[dict]:
    """Получаем свежие объявления, начиная с lastId (если задан).
    Возвращаем список объявлений с rentTime для фильтрации посуточных.
    """
    params = dict(INPARS_FILTERS)
    if last_seen_id:
        params["lastId"] = last_seen_id
        params["sortBy"] = "id_asc"  # при использовании lastId — только id_asc/id_desc

    # expand=rentTime,isApartments,rooms — нужны для финальной фильтрации и отображения
    params["expand"] = "rentTime,isApartments,rooms,district,metro"

    data = await inpars_request(client, "/estate", params)
    listings = data.get("data", [])
    logger.info(f"Inpars вернул {len(listings)} объявлений (после lastId={last_seen_id})")
    return listings


def passes_filters(listing: dict) -> bool:
    """Финальная фильтрация на нашей стороне (Inpars не умеет всё сам)."""
    # 1. Только длительная аренда (rentTime: 1=длит, 2=посуточно, 0=не указан)
    rent_time = listing.get("rentTime")
    if rent_time == 2:  # посуточно — отбрасываем
        return False

    # 2. Категория — квартира/апартаменты/студия (если справочник загрузился)
    if ALLOWED_CATEGORY_IDS and listing.get("categoryId") not in ALLOWED_CATEGORY_IDS:
        return False

    # 3. Двойной контроль "только собственник" — на случай если API кто-то "проскочил"
    if listing.get("agent") != 0:
        return False

    return True


# ---------- Telegram: формирование сообщения ----------
def format_listing_message(listing: dict) -> str:
    """Формирует красивое сообщение об объявлении для Telegram."""
    cost = listing.get("cost", 0)
    cost_str = f"{cost:,}".replace(",", " ") + " ₽/мес" if cost else "цена не указана"

    rooms = listing.get("rooms")
    is_apt = listing.get("isApartments")
    title = listing.get("title", "")

    # Тип объекта
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
        # phones — массив чисел, форматируем первый
        p = str(phones[0])
        if p.startswith("7") and len(p) == 11:
            phone_str = f"+7 ({p[1:4]}) {p[4:7]}-{p[7:9]}-{p[9:11]}"
        else:
            phone_str = p

    text = listing.get("text", "")
    if len(text) > 300:
        text = text[:297] + "…"

    parts = [
        f"💎 *{kind}*  ·  *{cost_str}*",
        f"📐 {sq} м²" + (f"  ·  {floor_str}" if floor_str else ""),
        f"📍 {address}",
    ]
    if location:
        parts.append(f"🚇 {location}")
    parts.append("")
    parts.append(f"_{text}_" if text else "")
    parts.append("")
    parts.append(f"👤 {name}" + (f"  ·  `{phone_str}`" if phone_str else ""))
    parts.append(f"🌐 [{source}]({url})")

    return "\n".join(p for p in parts if p is not None)


def make_action_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    """Кнопки под объявлением. callback_data вида 'lead:<action>:<listing_id>'."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Взять в работу",  callback_data=f"lead:take:{listing_id}"),
            InlineKeyboardButton("⏰ Перезвонить",     callback_data=f"lead:later:{listing_id}"),
        ],
        [
            InlineKeyboardButton("❌ Не подходит",     callback_data=f"lead:skip:{listing_id}"),
        ],
    ])


# ---------- Запись в Google Sheets ----------
async def send_to_sheets(client: httpx.AsyncClient, payload: dict) -> None:
    """Отправляет объявление в Google Sheets через Apps Script Webhook."""
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
    """Отправляет в Sheets команду: обновить статус по listing_id."""
    if not SHEETS_OBJECTS_WEBHOOK:
        return
    payload = {
        "action":     "update_status",
        "listing_id": listing_id,
        "status":     status,
        "manager":    manager,
        "updated":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        await client.post(
            SHEETS_OBJECTS_WEBHOOK, json=payload, timeout=15.0,
            follow_redirects=True,
        )
    except Exception:
        logger.exception("Не удалось обновить статус в Sheets")


# ---------- Дедупликация: помним последний обработанный id ----------
# Между перезапусками сервиса состояние теряется, и мы можем заново увидеть
# объявления, уже обработанные раньше. Чтобы этого избежать, при старте
# мы запросим у Inpars один объект и возьмём его id как стартовую точку
# (т.е. при первом запуске мы пропустим всё старое и начнём слушать только новое).
last_seen_id_state: dict[str, int] = {"value": 0}


async def get_initial_last_id(client: httpx.AsyncClient) -> int:
    """При первом запуске берём id самого свежего объявления, чтобы не лить старьё."""
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


# ---------- Главный цикл опроса ----------
async def polling_loop(application: Application) -> None:
    """Каждые POLL_INTERVAL_SECONDS опрашиваем Inpars и шлём свежие лиды."""
    async with httpx.AsyncClient() as client:
        # Загружаем справочник категорий один раз при старте
        global ALLOWED_CATEGORY_IDS
        try:
            ALLOWED_CATEGORY_IDS = await load_category_ids(client)
        except Exception:
            logger.exception("Не удалось загрузить категории — фильтрация по категориям отключена")
            ALLOWED_CATEGORY_IDS = set()

        # Берём стартовую точку
        try:
            last_seen_id_state["value"] = await get_initial_last_id(client)
        except Exception:
            logger.exception("Не удалось получить стартовый last_id")

        logger.info(f"Старт цикла опроса с интервалом {POLL_INTERVAL_SECONDS} с")

        while True:
            try:
                listings = await fetch_new_listings(client, last_seen_id_state["value"])

                # Сортируем по id_asc, чтобы обрабатывать в порядке появления
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

                    # Отправляем в Telegram
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
                        logger.exception(f"Не удалось отправить объект {listing_id} в Telegram")

                    # Параллельно — в Google Sheets
                    sheets_payload = build_sheets_payload(listing)
                    await send_to_sheets(client, sheets_payload)

                    # Не флудим — пауза между сообщениями
                    await asyncio.sleep(0.5)

                last_seen_id_state["value"] = new_max_id
                logger.info(f"Цикл завершён. Отправлено: {shipped}. last_seen_id={new_max_id}")

            except Exception:
                logger.exception("Ошибка в цикле опроса")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


def build_sheets_payload(listing: dict) -> dict:
    """Готовит словарь для записи в Sheets."""
    phones = listing.get("phones") or []
    phone = str(phones[0]) if phones else ""

    rent_terms = listing.get("rentTerms") or {}

    return {
        "action":      "add_listing",  # тип записи (Apps Script различит)
        "listing_id":  listing.get("id"),
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source":      SOURCE_LABELS.get(listing.get("sourceId"), listing.get("source", "")),
        "url":         listing.get("url", ""),
        "is_apt":      "Апартаменты" if listing.get("isApartments") else "Квартира",
        "rooms":       listing.get("rooms", ""),
        "sq":          listing.get("sq", ""),
        "floor":       listing.get("floor", ""),
        "floors":      listing.get("floors", ""),
        "cost":        listing.get("cost", ""),
        "address":     listing.get("address", ""),
        "district":    listing.get("district", ""),
        "metro":       listing.get("metro", ""),
        "name":        listing.get("name", ""),
        "phone":       phone,
        "text":        (listing.get("text", "") or "")[:500],
        "commission":  rent_terms.get("commission", ""),
        "deposit":     rent_terms.get("deposit", ""),
        "status":      "новый",
        "manager":     "",
        "comment":     "",
    }


# ---------- Обработчики кнопок Telegram ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия 'Взять в работу / Перезвонить / Не подходит'."""
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

    # Маппим action → статус для Sheets и подпись в Telegram
    actions = {
        "take":  ("в работе",      f"✅ Взял в работу: {manager_name}"),
        "later": ("перезвонить",   f"⏰ Перезвонить позже — {manager_name}"),
        "skip":  ("не подходит",   f"❌ Не подходит — {manager_name}"),
    }
    status, footer = actions.get(action, ("неизвестно", "?"))

    # Обновляем сообщение: добавляем подпись о действии и убираем кнопки
    new_text = (query.message.text_markdown or query.message.text or "") + f"\n\n— — —\n*{footer}*"
    try:
        await query.edit_message_text(
            text=new_text,
            parse_mode="Markdown",
            disable_web_page_preview=False,
            reply_markup=None,  # убираем кнопки
        )
    except Exception:
        logger.exception("Не удалось отредактировать сообщение после нажатия кнопки")

    # Шлём команду на обновление статуса в Sheets
    async with httpx.AsyncClient() as client:
        await update_sheet_status(client, listing_id, status, manager_name)


# ---------- Точка входа ----------
async def post_init(application: Application) -> None:
    """Запускаем фоновый цикл опроса после старта бота."""
    asyncio.create_task(polling_loop(application))
    logger.info("Inpars worker готов к работе")


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Кнопки lead:* (под объявлениями)
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^lead:"))

    logger.info("Запускаю Inpars worker...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

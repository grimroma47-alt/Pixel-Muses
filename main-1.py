from __future__ import annotations

import asyncio
import html
import logging
import os
import pickle
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    LabeledPrice,
    ReplyKeyboardMarkup,
)
from aiohttp import web

logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Налаштування (усе зі змінних середовища Render)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задано змінну середовища BOT_TOKEN")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DAILY_BONUS = int(os.getenv("DAILY_BONUS", "2"))  # монет за щоденний вхід
TOPUP_AMOUNTS = [50, 100, 250, 500]  # у зірках Telegram; 1 ⭐ = 1 монета
STATE_FILE = os.getenv("STATE_FILE", "state.pkl")  # запасне збереження, якщо немає бази
SAVE_INTERVAL = int(os.getenv("SAVE_INTERVAL", "30"))

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ---------------------------------------------------------------------------
# Дані (зберігаються в Postgres або у файлі)
# ---------------------------------------------------------------------------
users: dict[int, dict] = {}
collections: dict[int, dict] = {}  # id -> {"id","title","price","photos":[file_id],"active":bool}
col_counter = 0
total_stars = 0  # скільки зірок отримано за весь час (для статистики)

_save_event = asyncio.Event()


def request_save():
    _save_event.set()


def esc(v) -> str:
    return html.escape(str(v))


def get_user(user_id: int) -> dict:
    if user_id not in users:
        users[user_id] = {
            "balance": 0,
            "bought": set(),
            "last_daily": None,
            "joined": time.time(),
        }
    return users[user_id]


def today() -> str:
    return datetime.now(timezone(timedelta(hours=3))).date().isoformat()


# ---------------------------------------------------------------------------
# Збереження: Postgres (DATABASE_URL) або локальний файл
# ---------------------------------------------------------------------------
def _db_connect():
    import socket
    import ssl
    from urllib.parse import unquote, urlparse

    import pg8000.native

    url = urlparse(DATABASE_URL)
    host = url.hostname or "localhost"
    port = url.port or 5432
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        host = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except Exception:  # noqa: BLE001
        pass
    return pg8000.native.Connection(
        user=unquote(url.username or ""),
        password=unquote(url.password or ""),
        host=host,
        port=port,
        database=(url.path or "/postgres").lstrip("/") or "postgres",
        ssl_context=ctx,
        timeout=30,
    )


def _db_load() -> bytes | None:
    con = _db_connect()
    try:
        con.run("CREATE TABLE IF NOT EXISTS gallery_state (id INTEGER PRIMARY KEY, data BYTEA NOT NULL)")
        rows = con.run("SELECT data FROM gallery_state WHERE id = 1")
        return bytes(rows[0][0]) if rows else None
    finally:
        con.close()


def _db_save(blob: bytes):
    con = _db_connect()
    try:
        con.run(
            "INSERT INTO gallery_state (id, data) VALUES (1, :d) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
            d=blob,
        )
    finally:
        con.close()


def _snapshot() -> bytes:
    return pickle.dumps(
        {"users": users, "collections": collections, "col_counter": col_counter, "total_stars": total_stars},
        protocol=4,
    )


def _apply(blob: bytes):
    global col_counter, total_stars
    data = pickle.loads(blob)
    users.update(data.get("users", {}))
    collections.update(data.get("collections", {}))
    col_counter = data.get("col_counter", 0)
    total_stars = data.get("total_stars", 0)


async def load_state():
    blob = None
    if DATABASE_URL:
        for attempt in range(1, 6):
            try:
                blob = await asyncio.to_thread(_db_load)
                break
            except Exception as e:  # noqa: BLE001
                logging.warning("База недоступна (спроба %s/5): %s", attempt, e)
                await asyncio.sleep(3 * attempt)
        else:
            # краще не стартувати, ніж запуститись з порожніми даними і затерти базу
            raise SystemExit("❌ Не вдалося підключитися до бази даних")
    elif os.path.exists(STATE_FILE):
        with open(STATE_FILE, "rb") as f:
            blob = f.read()
    else:
        logging.warning("DATABASE_URL не задано — дані зникнуть після перезапуску Render.")
    if blob:
        _apply(blob)
        logging.info("Завантажено: %s користувачів, %s колекцій", len(users), len(collections))


_last_blob: bytes | None = None


async def save_state():
    global _last_blob
    blob = _snapshot()
    if blob == _last_blob:
        return
    try:
        if DATABASE_URL:
            await asyncio.to_thread(_db_save, blob)
        else:
            with open(STATE_FILE, "wb") as f:
                f.write(blob)
        _last_blob = blob
    except Exception as e:  # noqa: BLE001
        logging.error("Не вдалося зберегти дані: %s", e)


async def persist_loop():
    while True:
        try:
            await asyncio.wait_for(_save_event.wait(), timeout=SAVE_INTERVAL)
        except asyncio.TimeoutError:
            pass
        _save_event.clear()
        await save_state()


# ---------------------------------------------------------------------------
# Кнопки і клавіатури
# ---------------------------------------------------------------------------
BTN_GALLERY = "🖼 Галерея"
BTN_MINE = "📦 Мої покупки"
BTN_BALANCE = "👛 Баланс"
BTN_TOPUP = "💳 Поповнити"
BTN_DAILY = "🎁 Щоденний бонус"
BTN_HELP = "🆘 Допомога"


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_GALLERY)],
            [KeyboardButton(text=BTN_MINE), KeyboardButton(text=BTN_BALANCE)],
            [KeyboardButton(text=BTN_TOPUP), KeyboardButton(text=BTN_DAILY)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def topup_keyboard():
    rows = [[InlineKeyboardButton(text=f"⭐ {a} зірок → {a} монет", callback_data=f"top_{a}")] for a in TOPUP_AMOUNTS]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_admin(user_id: int | None) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


# ---------------------------------------------------------------------------
# Користувацькі команди
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    get_user(m.from_user.id)
    request_save()
    await m.answer(
        "👋 Привіт! Це галерея ШІ-зображень вигаданих персонажів.\n\n"
        "🖼 Відкривай колекції в «Галереї», перша картинка кожної — безкоштовний превʼю.\n"
        "💳 Поповнюй баланс зірками Telegram і купуй колекції.\n"
        f"🎁 Щодня можна забрати бонус: +{DAILY_BONUS} монет.\n\n"
        "Усі зображення створені ШІ, на них вигадані персонажі, не реальні люди.",
        reply_markup=main_keyboard(),
    )


@dp.message(F.text == BTN_HELP)
@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    await m.answer(
        "ℹ️ Як це працює:\n"
        "1. Поповни баланс (💳) зірками Telegram.\n"
        "2. У «🖼 Галереї» обери колекцію й натисни «Купити».\n"
        "3. Куплене завжди лежить у «📦 Мої покупки».\n\n"
        "Проблеми з оплатою: /paysupport"
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(m: types.Message):
    # Telegram вимагає, щоб бот із платежами мав цю команду
    await m.answer(
        "💬 Якщо оплата пройшла, а монети не зарахувались, напиши адміну й додай час платежу. "
        "Повернення зірок розглядаємо індивідуально."
    )
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"💬 /paysupport від <code>{m.from_user.id}</code>")
        except Exception as e:  # noqa: BLE001
            logging.warning("paysupport: %s", e)


@dp.message(F.text == BTN_BALANCE)
async def show_balance(m: types.Message):
    u = get_user(m.from_user.id)
    await m.answer(f"👛 Баланс: <b>{u['balance']}</b> монет\n📦 Куплено колекцій: {len(u['bought'])}")


@dp.message(F.text == BTN_TOPUP)
async def show_topup(m: types.Message):
    await m.answer("Обери суму поповнення (1 ⭐ = 1 монета):", reply_markup=topup_keyboard())


@dp.callback_query(F.data.startswith("top_"))
async def start_topup(cb: types.CallbackQuery):
    amount = int(cb.data.split("_")[1])
    if amount not in TOPUP_AMOUNTS:
        await cb.answer()
        return
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title=f"Поповнення на {amount} монет",
        description="Монети для покупки колекцій у галереї",
        payload=f"topup:{amount}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} монет", amount=amount)],
        provider_token="",
    )
    await cb.answer()


@dp.pre_checkout_query()
async def pre_checkout(q: types.PreCheckoutQuery):
    await q.answer(ok=q.invoice_payload.startswith("topup:"))


@dp.message(F.successful_payment)
async def paid(m: types.Message):
    global total_stars
    sp = m.successful_payment
    amount = int(sp.invoice_payload.split(":")[1])
    u = get_user(m.from_user.id)
    u["balance"] += amount
    total_stars += amount
    request_save()
    await m.answer(f"✅ Оплату отримано! Баланс: <b>{u['balance']}</b> монет.")
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"💰 +{amount} ⭐ від <code>{m.from_user.id}</code>")
        except Exception as e:  # noqa: BLE001
            logging.warning("notify admin: %s", e)


@dp.message(F.text == BTN_DAILY)
async def daily(m: types.Message):
    u = get_user(m.from_user.id)
    if u["last_daily"] == today():
        await m.answer("Сьогодні бонус уже забрано. Повертайся завтра 🙂")
        return
    u["last_daily"] = today()
    u["balance"] += DAILY_BONUS
    request_save()
    await m.answer(f"🎁 +{DAILY_BONUS} монет! Баланс: {u['balance']}")


# ---------------------------------------------------------------------------
# Галерея і покупки
# ---------------------------------------------------------------------------
async def deliver(chat_id: int, col: dict):
    """Надсилає всі фото колекції. protect_content забороняє пересилання й збереження."""
    photos = col["photos"]
    for i in range(0, len(photos), 10):
        chunk = photos[i : i + 10]
        if len(chunk) == 1:
            await bot.send_photo(chat_id, chunk[0], protect_content=True)
        else:
            await bot.send_media_group(chat_id, [InputMediaPhoto(media=p) for p in chunk], protect_content=True)


@dp.message(F.text == BTN_GALLERY)
async def show_gallery(m: types.Message):
    items = [c for c in collections.values() if c["active"] and c["photos"]]
    if not items:
        await m.answer("Поки що колекцій немає. Заходь пізніше!")
        return
    u = get_user(m.from_user.id)
    rows = []
    for c in sorted(items, key=lambda x: x["id"]):
        mark = "✅" if c["id"] in u["bought"] else f"{c['price']} 🪙"
        rows.append([InlineKeyboardButton(text=f"{c['title']} · {len(c['photos'])} фото · {mark}", callback_data=f"col_{c['id']}")])
    await m.answer("🖼 Колекції:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("col_"))
async def open_collection(cb: types.CallbackQuery):
    col = collections.get(int(cb.data.split("_")[1]))
    if not col or not col["active"] or not col["photos"]:
        await cb.answer("Колекція недоступна", show_alert=True)
        return
    u = get_user(cb.from_user.id)
    owned = col["id"] in u["bought"]
    btn = (
        InlineKeyboardButton(text="📥 Отримати знову", callback_data=f"get_{col['id']}")
        if owned
        else InlineKeyboardButton(text=f"🛒 Купити за {col['price']} 🪙", callback_data=f"buy_{col['id']}")
    )
    await cb.message.answer_photo(
        col["photos"][0],
        caption=f"<b>{esc(col['title'])}</b>\n{len(col['photos'])} фото · перше — превʼю\nТвій баланс: {u['balance']} 🪙",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn]]),
        protect_content=True,
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("buy_"))
async def buy(cb: types.CallbackQuery):
    col = collections.get(int(cb.data.split("_")[1]))
    if not col or not col["active"] or not col["photos"]:
        await cb.answer("Колекція недоступна", show_alert=True)
        return
    u = get_user(cb.from_user.id)
    if col["id"] in u["bought"]:
        await cb.answer("Вже куплено, дивись у «Мої покупки»", show_alert=True)
        return
    if u["balance"] < col["price"]:
        await cb.answer(f"Не вистачає монет: потрібно {col['price']}, у тебе {u['balance']}", show_alert=True)
        await cb.message.answer("Поповни баланс:", reply_markup=topup_keyboard())
        return
    u["balance"] -= col["price"]
    u["bought"].add(col["id"])
    col["sold"] = col.get("sold", 0) + 1
    request_save()
    await cb.answer("Куплено ✅")
    await cb.message.answer(f"✅ Куплено «{esc(col['title'])}». Залишок: {u['balance']} 🪙")
    await deliver(cb.from_user.id, col)


@dp.callback_query(F.data.startswith("get_"))
async def get_again(cb: types.CallbackQuery):
    col = collections.get(int(cb.data.split("_")[1]))
    u = get_user(cb.from_user.id)
    if not col or col["id"] not in u["bought"]:
        await cb.answer("Недоступно", show_alert=True)
        return
    await cb.answer()
    await deliver(cb.from_user.id, col)


@dp.message(F.text == BTN_MINE)
async def my_purchases(m: types.Message):
    u = get_user(m.from_user.id)
    owned = [collections[i] for i in sorted(u["bought"]) if i in collections and collections[i]["photos"]]
    if not owned:
        await m.answer("Покупок поки немає. Загляни в «🖼 Галерею».")
        return
    rows = [[InlineKeyboardButton(text=f"📥 {c['title']}", callback_data=f"get_{c['id']}")] for c in owned]
    await m.answer("📦 Твої колекції:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# ---------------------------------------------------------------------------
# Адмінка: тільки для ADMIN_ID
# ---------------------------------------------------------------------------
class AdminStates(StatesGroup):
    photos = State()


ADMIN_HELP = (
    "🛠 <b>Адмінка</b>\n\n"
    "/newcol Назва | ціна — створити колекцію\n"
    "/addphotos ID — почати додавати фото (кидай фото, потім /done)\n"
    "/cols — список колекцій\n"
    "/price ID ціна — змінити ціну\n"
    "/hide ID, /show ID — сховати або показати\n"
    "/delcol ID — видалити\n"
    "/give USER_ID сума — нарахувати монети\n"
    "/stats — статистика\n\n"
    "⚠️ Заливай лише ШІ-зображення вигаданих персонажів в одязі. Реальних людей і оголеного тіла в боті не має бути: Telegram такі боти блокує."
)


@dp.message(Command("admin"))
async def cmd_admin(m: types.Message):
    if is_admin(m.from_user.id):
        await m.answer(ADMIN_HELP)


@dp.message(Command("newcol"))
async def cmd_newcol(m: types.Message):
    global col_counter
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split(maxsplit=1)
    try:
        title, price = parts[1].rsplit("|", 1)
        title, price = title.strip(), int(price.strip())
        assert title and price >= 0
    except Exception:  # noqa: BLE001
        await m.answer("Формат: /newcol Назва | ціна\nНаприклад: /newcol Літо в місті | 50")
        return
    col_counter += 1
    collections[col_counter] = {"id": col_counter, "title": title, "price": price, "photos": [], "active": False, "sold": 0}
    request_save()
    await m.answer(f"✅ Колекцію #{col_counter} створено (прихована, поки немає фото).\nДодай фото: /addphotos {col_counter}")


@dp.message(Command("addphotos"))
async def cmd_addphotos(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    try:
        col_id = int((m.text or "").split()[1])
        assert col_id in collections
    except Exception:  # noqa: BLE001
        await m.answer("Формат: /addphotos ID (ID дивись у /cols)")
        return
    await state.set_state(AdminStates.photos)
    await state.update_data(col_id=col_id)
    await m.answer(f"📸 Кидай фото для колекції #{col_id} (по одному або кількома). Коли все, напиши /done.")


@dp.message(AdminStates.photos, F.photo)
async def admin_photo(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    col = collections.get(data.get("col_id"))
    if not col:
        await state.clear()
        return
    col["photos"].append(m.photo[-1].file_id)
    col["active"] = True
    request_save()


@dp.message(AdminStates.photos, Command("done"))
async def admin_done(m: types.Message, state: FSMContext):
    data = await state.get_data()
    col = collections.get(data.get("col_id"))
    await state.clear()
    if col:
        await m.answer(f"✅ У колекції #{col['id']} «{esc(col['title'])}» тепер {len(col['photos'])} фото, ціна {col['price']} 🪙.")


@dp.message(Command("cols"))
async def cmd_cols(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    if not collections:
        await m.answer("Колекцій немає. Створи: /newcol Назва | ціна")
        return
    lines = [
        f"#{c['id']} {esc(c['title'])} · {len(c['photos'])} фото · {c['price']} 🪙 · продано {c.get('sold', 0)} · {'👁' if c['active'] else '🙈'}"
        for c in sorted(collections.values(), key=lambda x: x["id"])
    ]
    await m.answer("\n".join(lines))


def _arg_int(m: types.Message, n: int = 1) -> int | None:
    try:
        return int((m.text or "").split()[n])
    except Exception:  # noqa: BLE001
        return None


@dp.message(Command("price"))
async def cmd_price(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    col_id, price = _arg_int(m, 1), _arg_int(m, 2)
    if col_id not in collections or price is None or price < 0:
        await m.answer("Формат: /price ID ціна")
        return
    collections[col_id]["price"] = price
    request_save()
    await m.answer("✅ Ціну змінено")


@dp.message(Command("hide"))
@dp.message(Command("show"))
async def cmd_visibility(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    col_id = _arg_int(m)
    if col_id not in collections:
        await m.answer("Формат: /hide ID або /show ID")
        return
    collections[col_id]["active"] = (m.text or "").startswith("/show")
    request_save()
    await m.answer("✅ Готово")


@dp.message(Command("delcol"))
async def cmd_delcol(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    col_id = _arg_int(m)
    if col_id not in collections:
        await m.answer("Формат: /delcol ID")
        return
    collections.pop(col_id)
    request_save()
    await m.answer("🗑 Видалено")


@dp.message(Command("give"))
async def cmd_give(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    uid, amount = _arg_int(m, 1), _arg_int(m, 2)
    if uid is None or amount is None:
        await m.answer("Формат: /give USER_ID сума")
        return
    get_user(uid)["balance"] += amount
    request_save()
    await m.answer(f"✅ Нараховано {amount} 🪙 користувачу {uid}")


@dp.message(Command("stats"))
async def cmd_stats(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    sold = sum(c.get("sold", 0) for c in collections.values())
    await m.answer(
        f"📊 Користувачів: {len(users)}\n"
        f"🖼 Колекцій: {len(collections)}\n"
        f"🛒 Продано: {sold}\n"
        f"⭐ Отримано зірок: {total_stars}"
    )


# ---------------------------------------------------------------------------
# Запуск (веб-сервер потрібен, щоб Render бачив відкритий порт)
# ---------------------------------------------------------------------------
async def start_web():
    async def health(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()


async def main():
    await load_state()
    await start_web()
    saver = asyncio.create_task(persist_loop())
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        saver.cancel()
        await save_state()


if __name__ == "__main__":
    asyncio.run(main())

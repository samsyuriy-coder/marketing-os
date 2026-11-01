"""Telegram-бот учёта кафе: приготовлено → остатки → касса → расходы → итог дня."""
import asyncio
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from dotenv import load_dotenv

from db import DB
from export import export_xlsx
from report import build, format_text, money, num

load_dotenv()
TZ = ZoneInfo(os.getenv("TZ", "Asia/Almaty"))
OWNERS = {int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()}
STAFF = {int(x) for x in os.getenv("STAFF_IDS", "").split(",") if x.strip()} | OWNERS
db = DB(os.getenv("DB_PATH", "cafe.db"))
db.load_menu_csv(os.path.join(os.path.dirname(__file__), "menu.csv"))
router = Router()

B_PREP, B_LEFT, B_EXP, B_CASH, B_REPORT = "🍳 Приготовили", "📦 Остатки", "💸 Расход", "💰 Касса", "📊 Итог дня"
KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=B_PREP), KeyboardButton(text=B_LEFT)],
              [KeyboardButton(text=B_EXP), KeyboardButton(text=B_CASH)],
              [KeyboardButton(text=B_REPORT)]], resize_keyboard=True)
CATEGORIES = ["Закупка", "Зарплата", "Прочее"]


class S(StatesGroup):
    qty = State()
    expense = State()
    cash = State()
    kaspi = State()
    drawer = State()


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def parse_number(text: str):
    m = re.search(r"-?\d+(?:[.,]\d+)?", text.replace(" ", ""))
    return float(m.group().replace(",", ".")) if m else None


def allowed(uid: int) -> bool:
    return uid in STAFF


def items_kb(mode: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for it in db.items():
        row.append(InlineKeyboardButton(text=it["name"], callback_data=f"{mode}:{it['id']}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"Ваш Telegram ID: {m.from_user.id}")


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    if not allowed(m.from_user.id):
        return await m.answer("Нет доступа. Отправьте владельцу ваш ID: " + str(m.from_user.id))
    await m.answer("Выберите действие 👇", reply_markup=KB)


# ---------- приготовлено / остатки
@router.message(F.text.in_({B_PREP, B_LEFT}))
async def pick_item(m: Message, state: FSMContext):
    if not allowed(m.from_user.id):
        return
    await state.clear()
    mode = "prep" if m.text == B_PREP else "left"
    title = "Что приготовили?" if mode == "prep" else "Остаток по какому блюду?"
    await m.answer(title, reply_markup=items_kb(mode))


@router.callback_query(F.data.regexp(r"^(prep|left):\d+$"))
async def item_chosen(c: CallbackQuery, state: FSMContext):
    if not allowed(c.from_user.id):
        return await c.answer()
    mode, item_id = c.data.split(":")
    it = db.item(int(item_id))
    await state.set_state(S.qty)
    await state.update_data(mode=mode, item_id=int(item_id))
    q = "Сколько приготовили" if mode == "prep" else "Сколько осталось"
    await c.message.answer(f"{it['name']}: {q}? ({it['unit']})")
    await c.answer()


@router.message(S.qty)
async def got_qty(m: Message, state: FSMContext):
    qty = parse_number(m.text or "")
    if qty is None or qty < 0:
        return await m.answer("Напишите число, например 25 или 1.5")
    d = await state.get_data()
    it = db.item(d["item_id"])
    if d["mode"] == "prep":
        db.add_prepared(today(), d["item_id"], qty)
        txt = f"✅ {it['name']}: +{num(qty)}"
    else:
        db.set_left(today(), d["item_id"], qty)
        txt = f"✅ {it['name']}: остаток {num(qty)}"
    await state.clear()
    await m.answer(txt + "\nЕщё?", reply_markup=items_kb(d["mode"]))


# ---------- расходы
@router.message(F.text == B_EXP)
async def exp_cat(m: Message, state: FSMContext):
    if not allowed(m.from_user.id):
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=c, callback_data=f"cat:{c}") for c in CATEGORIES]])
    await m.answer("Категория расхода:", reply_markup=kb)


@router.callback_query(F.data.startswith("cat:"))
async def exp_cat_chosen(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.expense)
    await state.update_data(cat=c.data[4:])
    await c.message.answer("Напишите сумму и комментарий, например: 5000 такси")
    await c.answer()


@router.message(S.expense)
async def exp_saved(m: Message, state: FSMContext):
    amount = parse_number(m.text or "")
    if amount is None or amount <= 0:
        return await m.answer("Не вижу суммы. Пример: 5000 такси")
    note = re.sub(r"-?\d+(?:[.,]\d+)?", "", m.text, count=1).strip()
    cat = (await state.get_data())["cat"]
    db.add_expense(today(), cat, amount, note, m.from_user.id)
    await state.clear()
    await m.answer(f"✅ {cat}: {money(amount)} {note}\nОтменить последнее: /undo", reply_markup=KB)


@router.message(Command("undo"))
async def undo(m: Message):
    if not allowed(m.from_user.id):
        return
    row = db.undo_last_expense(today(), m.from_user.id)
    await m.answer(f"↩️ Удалено: {row['category']} {money(row['amount'])} {row['note']}" if row else "Нечего отменять")


# ---------- касса
@router.message(F.text == B_CASH)
async def cash_start(m: Message, state: FSMContext):
    if not allowed(m.from_user.id):
        return
    await state.set_state(S.cash)
    await m.answer("Сколько наличными за день?")


@router.message(S.cash)
async def cash_got(m: Message, state: FSMContext):
    v = parse_number(m.text or "")
    if v is None:
        return await m.answer("Напишите число")
    db.set_cash(today(), cash=v)
    await state.set_state(S.kaspi)
    await m.answer("Сколько по Kaspi?")


@router.message(S.kaspi)
async def kaspi_got(m: Message, state: FSMContext):
    v = parse_number(m.text or "")
    if v is None:
        return await m.answer("Напишите число")
    db.set_cash(today(), kaspi=v)
    await state.set_state(S.drawer)
    await m.answer("Сколько оставили в кассе? (0 — если ничего)")


@router.message(S.drawer)
async def drawer_got(m: Message, state: FSMContext):
    v = parse_number(m.text or "")
    if v is None:
        return await m.answer("Напишите число")
    db.set_cash(today(), drawer=v)
    await state.clear()
    await m.answer("✅ Касса записана. Нажмите «📊 Итог дня»", reply_markup=KB)


# ---------- итог
def day_report_text(day: str) -> str:
    return format_text(build(day, db.stock_rows(day), db.expenses(day), db.cash_row(day)))


@router.message(F.text == B_REPORT)
@router.message(Command("report"))
async def report(m: Message):
    if m.from_user.id not in OWNERS:
        return await m.answer("Итог дня доступен владельцу.")
    await m.answer(day_report_text(today()))


@router.message(Command("export"))
async def export(m: Message):
    if m.from_user.id not in OWNERS:
        return
    path = "export.xlsx"
    export_xlsx(db, path)
    await m.answer_document(FSInputFile(path), caption="Все дни в Excel")


# ---------- меню (владелец)
@router.message(Command("menu"))
async def show_menu(m: Message):
    if m.from_user.id not in OWNERS:
        return
    lines = [f"{it['name']} — {money(it['price'])}/{it['unit']}" for it in db.items()]
    await m.answer("\n".join(lines) + "\n\nДобавить/изменить: /add Название;цена;ед\nУбрать: /del Название")


@router.message(Command("add"))
async def add_item(m: Message):
    if m.from_user.id not in OWNERS:
        return
    parts = [p.strip() for p in (m.text.partition(" ")[2]).split(";")]
    if len(parts) < 2 or parse_number(parts[1]) is None:
        return await m.answer("Формат: /add Название;цена;ед   Пример: /add Лагман;1800;порц")
    db.upsert_item(parts[0], parse_number(parts[1]), parts[2] if len(parts) > 2 else "шт")
    await m.answer(f"✅ {parts[0]} — {money(parse_number(parts[1]))}")


@router.message(Command("del"))
async def del_item(m: Message):
    if m.from_user.id not in OWNERS:
        return
    name = m.text.partition(" ")[2].strip()
    await m.answer("✅ Убрано" if db.deactivate_item(name) else "Не нашёл такого блюда")


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(os.environ["BOT_TOKEN"])
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

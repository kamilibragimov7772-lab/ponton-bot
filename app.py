import os
import json
import asyncio
import threading
import time as time_module
from flask import Flask, request, jsonify, send_from_directory
from telegram import (
    Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import Application
import db

app = Flask(__name__, static_folder="static")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://ponton-bot-production.up.railway.app")

db.init_db()

# ─── USER STATE MACHINE ───────────────────────────────────────────────────────
user_states = {}  # {chat_id: {"step": ..., "data": {}}}

def get_state(cid):
    return user_states.get(cid, {"step": None, "data": {}})

def set_state(cid, step, data=None):
    user_states[cid] = {"step": step, "data": data or {}}

def clear_state(cid):
    user_states.pop(cid, None)

# ─── KEYBOARDS ────────────────────────────────────────────────────────────────
def admin_main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить сеанс", callback_data="add_session")],
        [InlineKeyboardButton("📋 Список сеансов", callback_data="list_sessions")],
        [InlineKeyboardButton("🎫 Все брони", callback_data="all_bookings")],
    ])

def back_kb(callback="admin_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=callback)]])

def session_manage_kb(sid, is_active):
    toggle = "❌ Отключить" if is_active else "✅ Включить"
    toggle_cb = f"toggle_{sid}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Название", callback_data=f"edit_movie_{sid}"),
         InlineKeyboardButton("📅 Дата", callback_data=f"edit_date_{sid}")],
        [InlineKeyboardButton("🕐 Время", callback_data=f"edit_time_{sid}"),
         InlineKeyboardButton("💰 Цена", callback_data=f"edit_price_{sid}")],
        [InlineKeyboardButton("💺 Мест", callback_data=f"edit_seats_{sid}"),
         InlineKeyboardButton(toggle, callback_data=toggle_cb)],
        [InlineKeyboardButton("📋 Брони сеанса", callback_data=f"session_bookings_{sid}")],
        [InlineKeyboardButton("🗑 Удалить сеанс", callback_data=f"delete_confirm_{sid}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="list_sessions")],
    ])

def booking_manage_kb(bid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"confirm_pay_{bid}")],
        [InlineKeyboardButton("❌ Отменить бронь", callback_data=f"cancel_booking_{bid}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="all_bookings")],
    ])

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def session_text(s):
    status = "✅ Активен" if s["is_active"] else "❌ Неактивен"
    return (f"🎬 *{s['movie']}*\n"
            f"📅 {s['date']} в {s['time']}\n"
            f"💰 {s['price']} ₽ за место\n"
            f"💺 Мест: {s['total_seats']}\n"
            f"Статус: {status}")

def booking_text(b, show_session=True):
    status_map = {
        "pending": "⏳ Ожидает оплаты",
        "paid": "✅ Оплачено",
        "cancelled": "❌ Отменено"
    }
    txt = f"🎫 Бронь #{b['id']}\n"
    if show_session:
        txt += f"🎬 {b.get('movie', '')} {b.get('date', '')} {b.get('time', '')}\n"
    txt += (f"👤 {b['first_name']} {b['last_name']}\n"
            f"📱 {b['phone']}\n"
            f"💺 Места: {b['seats']}\n"
            f"💰 {b['total_price']} ₽\n"
            f"Статус: {status_map.get(b['status'], b['status'])}")
    return txt

async def send_all_admins(text, reply_markup=None):
    bot = Bot(token=BOT_TOKEN)
    for aid in db.get_all_admins():
        try:
            await bot.send_message(chat_id=aid, text=text, parse_mode="Markdown",
                                   reply_markup=reply_markup)
        except Exception as e:
            print(f"Error sending to admin {aid}: {e}")

# ─── BOT HANDLERS ─────────────────────────────────────────────────────────────
async def handle_message(update_data):
    bot = Bot(token=BOT_TOKEN)
    upd = Update.de_json(update_data, bot)

    # Callback query
    if upd.callback_query:
        await handle_callback(bot, upd.callback_query)
        return

    if not upd.message:
        return

    msg = upd.message
    cid = msg.chat_id
    text = msg.text or ""
    is_admin = db.is_admin(cid)
    state = get_state(cid)

    # /start
    if text == "/start":
        clear_state(cid)
        if is_admin:
            await bot.send_message(
                cid, "👋 Привет, администратор!\n\nВыбери действие:",
                reply_markup=admin_main_kb()
            )
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Открыть кассу", web_app=WebAppInfo(url=WEBAPP_URL))
            ]])
            await bot.send_message(
                cid,
                "🎬 *Понтон — кино на воде*\n\nНажми кнопку чтобы выбрать места и забронировать.",
                reply_markup=kb, parse_mode="Markdown"
            )
        return

    # Admin state machine
    if is_admin and state["step"]:
        await handle_admin_input(bot, cid, text, state)
        return

    # Web App data
    if msg.web_app_data:
        await handle_webapp_data(bot, msg)
        return

    # Default
    if not is_admin:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Открыть кассу", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
        await bot.send_message(cid, "Нажми кнопку чтобы открыть кассу:", reply_markup=kb)

async def handle_webapp_data(bot, msg):
    cid = msg.chat_id
    try:
        data = json.loads(msg.web_app_data.data)
        sid = data["session_id"]
        seats = data["seats"]
        first_name = data["first_name"]
        last_name = data["last_name"]
        phone = data["phone"]
        tg_id = data.get("telegram_id", cid)
        username = data.get("username", "")

        session = db.get_session(sid)
        if not session:
            await bot.send_message(cid, "❌ Сеанс не найден.")
            return

        price = session["price"] * len(seats)
        bid = db.create_booking(sid, tg_id, username, first_name, last_name, phone, seats, price)

        if bid is None:
            await bot.send_message(cid, "❌ Одно из выбранных мест уже занято. Попробуйте снова.")
            return

        seats_str = ", ".join(str(s) for s in seats)
        # Notify client
        await bot.send_message(
            cid,
            f"✅ *Бронь создана!*\n\n"
            f"🎬 {session['movie']}\n"
            f"📅 {session['date']} в {session['time']}\n"
            f"💺 Места: {seats_str}\n"
            f"💰 Сумма: {price} ₽\n"
            f"⏳ Статус: ожидает подтверждения оплаты\n\n"
            f"Мы свяжемся с вами для подтверждения.",
            parse_mode="Markdown"
        )

        # Notify admins
        admin_text = (
            f"🔔 *Новая бронь #{bid}*\n\n"
            f"🎬 {session['movie']} — {session['date']} {session['time']}\n"
            f"👤 {first_name} {last_name}\n"
            f"📱 {phone}\n"
            f"💺 Места: {seats_str}\n"
            f"💰 {price} ₽"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"confirm_pay_{bid}"),
            InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_booking_{bid}")
        ]])
        await send_all_admins(admin_text, kb)

    except Exception as e:
        print(f"webapp error: {e}")
        await bot.send_message(cid, "❌ Ошибка при создании брони.")

async def handle_callback(bot, cb):
    cid = cb.message.chat_id
    data = cb.data
    msg_id = cb.message.message_id

    await cb.answer()

    # ── Admin main ──
    if data == "admin_main":
        await bot.edit_message_text("Выбери действие:", cid, msg_id,
                                    reply_markup=admin_main_kb())

    # ── Add session ──
    elif data == "add_session":
        set_state(cid, "add_movie", {})
        await bot.edit_message_text(
            "🎬 Введите название фильма:\n\n_(или нажмите /cancel для отмены)_",
            cid, msg_id, parse_mode="Markdown",
            reply_markup=back_kb("admin_main")
        )

    # ── List sessions ──
    elif data == "list_sessions":
        sessions = db.get_sessions()
        if not sessions:
            await bot.edit_message_text("Сеансов нет. Добавьте первый!",
                                        cid, msg_id, reply_markup=back_kb("admin_main"))
            return
        buttons = []
        for s in sessions:
            icon = "✅" if s["is_active"] else "❌"
            buttons.append([InlineKeyboardButton(
                f"{icon} {s['movie']} — {s['date']} {s['time']}",
                callback_data=f"manage_session_{s['id']}"
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_main")])
        await bot.edit_message_text("📋 Все сеансы:", cid, msg_id,
                                    reply_markup=InlineKeyboardMarkup(buttons))

    # ── Manage session ──
    elif data.startswith("manage_session_"):
        sid = int(data.split("_")[-1])
        s = db.get_session(sid)
        if not s:
            await bot.edit_message_text("Сеанс не найден.", cid, msg_id)
            return
        await bot.edit_message_text(
            session_text(s), cid, msg_id,
            reply_markup=session_manage_kb(sid, s["is_active"]),
            parse_mode="Markdown"
        )

    # ── Toggle session ──
    elif data.startswith("toggle_"):
        sid = int(data.split("_")[-1])
        s = db.get_session(sid)
        new_val = 0 if s["is_active"] else 1
        db.update_session(sid, "is_active", new_val)
        s = db.get_session(sid)
        await bot.edit_message_text(
            session_text(s), cid, msg_id,
            reply_markup=session_manage_kb(sid, s["is_active"]),
            parse_mode="Markdown"
        )

    # ── Edit fields ──
    elif data.startswith("edit_"):
        parts = data.split("_")
        field = parts[1]
        sid = int(parts[2])
        prompts = {
            "movie": "Введите новое название фильма:",
            "date": "Введите новую дату (формат: 2024-07-15):",
            "time": "Введите новое время (формат: 20:00):",
            "price": "Введите новую цену (число, рублей):",
            "seats": "Введите новое количество мест:",
        }
        set_state(cid, f"edit_{field}", {"sid": sid, "msg_id": msg_id})
        await bot.edit_message_text(
            prompts.get(field, "Введите значение:"),
            cid, msg_id,
            reply_markup=back_kb(f"manage_session_{sid}")
        )

    # ── Delete confirm ──
    elif data.startswith("delete_confirm_"):
        sid = int(data.split("_")[-1])
        await bot.edit_message_text(
            "⚠️ Вы уверены что хотите удалить сеанс? Все брони также будут удалены.",
            cid, msg_id,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"delete_yes_{sid}")],
                [InlineKeyboardButton("◀️ Нет, назад", callback_data=f"manage_session_{sid}")]
            ])
        )

    elif data.startswith("delete_yes_"):
        sid = int(data.split("_")[-1])
        db.delete_session(sid)
        await bot.edit_message_text("✅ Сеанс удалён.", cid, msg_id,
                                    reply_markup=back_kb("list_sessions"))

    # ── All bookings ──
    elif data == "all_bookings":
        bookings = db.get_all_bookings()
        if not bookings:
            await bot.edit_message_text("Броней нет.", cid, msg_id,
                                        reply_markup=back_kb("admin_main"))
            return
        status_icons = {"pending": "⏳", "paid": "✅", "cancelled": "❌"}
        buttons = []
        for b in bookings:
            icon = status_icons.get(b["status"], "")
            buttons.append([InlineKeyboardButton(
                f"{icon} #{b['id']} {b['first_name']} {b['last_name']} — {b['movie']}",
                callback_data=f"booking_detail_{b['id']}"
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_main")])
        await bot.edit_message_text("🎫 Все брони:", cid, msg_id,
                                    reply_markup=InlineKeyboardMarkup(buttons))

    # ── Session bookings ──
    elif data.startswith("session_bookings_"):
        sid = int(data.split("_")[-1])
        bookings = db.get_bookings_for_session(sid)
        s = db.get_session(sid)
        if not bookings:
            await bot.edit_message_text(
                f"На сеанс «{s['movie']}» броней нет.",
                cid, msg_id,
                reply_markup=back_kb(f"manage_session_{sid}")
            )
            return
        status_icons = {"pending": "⏳", "paid": "✅", "cancelled": "❌"}
        buttons = []
        for b in bookings:
            icon = status_icons.get(b["status"], "")
            buttons.append([InlineKeyboardButton(
                f"{icon} #{b['id']} {b['first_name']} {b['last_name']} — {b['seats']}",
                callback_data=f"booking_detail_{b['id']}"
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data=f"manage_session_{sid}")])
        await bot.edit_message_text(
            f"Брони на «{s['movie']}»:", cid, msg_id,
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    # ── Booking detail ──
    elif data.startswith("booking_detail_"):
        bid = int(data.split("_")[-1])
        b = db.get_booking(bid)
        if not b:
            await bot.edit_message_text("Бронь не найдена.", cid, msg_id)
            return
        s = db.get_session(b["session_id"])
        b["movie"] = s["movie"] if s else "?"
        b["date"] = s["date"] if s else "?"
        b["time"] = s["time"] if s else "?"
        await bot.edit_message_text(
            booking_text(b), cid, msg_id,
            reply_markup=booking_manage_kb(bid),
            parse_mode="Markdown"
        )

    # ── Confirm payment ──
    elif data.startswith("confirm_pay_"):
        bid = int(data.split("_")[-1])
        b = db.get_booking(bid)
        if not b:
            await cb.answer("Бронь не найдена", show_alert=True)
            return
        db.update_booking_status(bid, "paid")
        s = db.get_session(b["session_id"])

        # Notify client
        if b["telegram_id"]:
            try:
                await bot.send_message(
                    b["telegram_id"],
                    f"🎉 *Оплата подтверждена!*\n\n"
                    f"🎬 {s['movie']}\n"
                    f"📅 {s['date']} в {s['time']}\n"
                    f"💺 Места: {b['seats']}\n"
                    f"💰 {b['total_price']} ₽\n"
                    f"✅ Статус: Куплено\n\n"
                    f"Ждём вас на Понтоне! 🌊",
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Cannot notify client: {e}")

        await bot.edit_message_text(
            f"✅ Оплата подтверждена для брони #{bid}\n{b['first_name']} {b['last_name']}",
            cid, msg_id,
            reply_markup=back_kb("all_bookings")
        )

    # ── Cancel booking ──
    elif data.startswith("cancel_booking_"):
        bid = int(data.split("_")[-1])
        b = db.get_booking(bid)
        if not b:
            await cb.answer("Бронь не найдена", show_alert=True)
            return
        db.update_booking_status(bid, "cancelled")

        if b["telegram_id"]:
            try:
                s = db.get_session(b["session_id"])
                await bot.send_message(
                    b["telegram_id"],
                    f"❌ *Бронь отменена*\n\n"
                    f"🎬 {s['movie']}\n"
                    f"📅 {s['date']} в {s['time']}\n"
                    f"💺 Места: {b['seats']}\n\n"
                    f"По вопросам: wa.me/79996422291",
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Cannot notify client: {e}")

        await bot.edit_message_text(
            f"❌ Бронь #{bid} отменена.",
            cid, msg_id,
            reply_markup=back_kb("all_bookings")
        )

async def handle_admin_input(bot, cid, text, state):
    step = state["step"]
    data = state["data"]

    if text == "/cancel":
        clear_state(cid)
        await bot.send_message(cid, "Отменено.", reply_markup=admin_main_kb())
        return

    # ── Add session flow ──
    if step == "add_movie":
        set_state(cid, "add_date", {"movie": text})
        await bot.send_message(cid, f"📅 Дата сеанса (формат: 2024-07-15):")
    elif step == "add_date":
        data["date"] = text
        set_state(cid, "add_time", data)
        await bot.send_message(cid, "🕐 Время сеанса (формат: 20:00):")
    elif step == "add_time":
        data["time"] = text
        set_state(cid, "add_price", data)
        await bot.send_message(cid, "💰 Цена билета (только число, рублей):")
    elif step == "add_price":
        try:
            data["price"] = int(text)
        except:
            await bot.send_message(cid, "❌ Введите число.")
            return
        set_state(cid, "add_seats", data)
        await bot.send_message(cid, "💺 Количество мест:")
    elif step == "add_seats":
        try:
            data["seats"] = int(text)
        except:
            await bot.send_message(cid, "❌ Введите число.")
            return
        sid = db.create_session(data["movie"], data["date"], data["time"],
                                data["price"], data["seats"])
        clear_state(cid)
        await bot.send_message(
            cid,
            f"✅ Сеанс добавлен!\n\n"
            f"🎬 {data['movie']}\n"
            f"📅 {data['date']} в {data['time']}\n"
            f"💰 {data['price']} ₽\n"
            f"💺 {data['seats']} мест",
            reply_markup=admin_main_kb()
        )

    # ── Edit fields ──
    elif step.startswith("edit_"):
        field = step.replace("edit_", "")
        sid = data["sid"]
        val = text
        if field in ["price", "seats"]:
            try:
                val = int(text)
            except:
                await bot.send_message(cid, "❌ Введите число.")
                return
        field_map = {"movie": "movie", "date": "date", "time": "time",
                     "price": "price", "seats": "total_seats"}
        db.update_session(sid, field_map.get(field, field), val)
        clear_state(cid)
        s = db.get_session(sid)
        await bot.send_message(
            cid, f"✅ Обновлено!\n\n{session_text(s)}",
            reply_markup=session_manage_kb(sid, s["is_active"]),
            parse_mode="Markdown"
        )

# ─── SCHEDULER ────────────────────────────────────────────────────────────────
def run_scheduler():
    while True:
        try:
            sessions = db.get_sessions_starting_in(55, 65)
            for s in sessions:
                asyncio.run(send_session_summary(s))
        except Exception as e:
            print(f"Scheduler error: {e}")
        time_module.sleep(60)

async def send_session_summary(s):
    bookings = db.get_bookings_for_session(s["id"])
    booked_map = {}
    for b in bookings:
        for seat in b["seats"].split(","):
            booked_map[int(seat.strip())] = b

    lines = [f"🎬 *{s['movie']}*\n📅 {s['date']} в {s['time']}\n\n📋 Список мест:\n"]
    for i in range(1, s["total_seats"] + 1):
        if i in booked_map:
            b = booked_map[i]
            lines.append(f"💺 Место {i} — {b['first_name']} {b['last_name']} — {b['phone']}")
        else:
            lines.append(f"💺 Место {i} — —")

    text = "\n".join(lines)
    await send_all_admins(text)

# Start scheduler thread
scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()

# ─── FLASK ROUTES ─────────────────────────────────────────────────────────────
@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    asyncio.run(handle_message(request.json))
    return "ok"

@app.route("/api/sessions")
def api_sessions():
    sessions = db.get_sessions(active_only=True)
    return jsonify(sessions)

@app.route("/api/seats/<int:session_id>")
def api_seats(session_id):
    taken = db.get_taken_seats(session_id)
    return jsonify({"taken": taken})

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/book", methods=["POST"])
def api_book():
    data = request.json
    sid = data.get("session_id")
    seats = data.get("seats", [])
    first_name = data.get("first_name", "").strip()
    last_name = data.get("last_name", "").strip()
    phone = data.get("phone", "").strip()
    tg_id = data.get("telegram_id")
    username = data.get("username", "")

    if not all([sid, seats, first_name, last_name, phone]):
        return jsonify({"ok": False, "error": "Заполните все поля"}), 400

    session = db.get_session(sid)
    if not session:
        return jsonify({"ok": False, "error": "Сеанс не найден"}), 404

    price = session["price"] * len(seats)
    bid = db.create_booking(sid, tg_id, username, first_name, last_name, phone, seats, price)

    if bid is None:
        return jsonify({"ok": False, "error": "Одно из мест уже занято"}), 409

    seats_str = ", ".join(str(s) for s in seats)
    admin_text = (
        f"🔔 *Новая бронь #{bid}*\n\n"
        f"🎬 {session['movie']} — {session['date']} {session['time']}\n"
        f"👤 {first_name} {last_name}\n"
        f"📱 {phone}\n"
        f"💺 Места: {seats_str}\n"
        f"💰 {price} ₽"
    )
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"confirm_pay_{bid}"),
        InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_booking_{bid}")
    ]])
    asyncio.run(send_all_admins(admin_text, kb))
    return jsonify({"ok": True, "booking_id": bid, "total": price})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

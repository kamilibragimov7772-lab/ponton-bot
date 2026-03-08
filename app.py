import os
import json
import asyncio
import threading
import time as time_module
from flask import Flask, request, jsonify, send_from_directory
from telegram import (
    Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo
)
import db

app = Flask(__name__, static_folder="static")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://ponton-bot-production.up.railway.app").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

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
    return (
        f"🎬 {s['movie']}\n"
        f"📅 {s['date']} в {s['time']}\n"
        f"💰 {s['price']} ₽ за место\n"
        f"💺 Мест: {s['total_seats']}\n"
        f"Статус: {status}"
    )

def booking_text(b, show_session=True):
    status_map = {
        "pending": "⏳ Ожидает оплаты",
        "paid": "✅ Оплачено",
        "cancelled": "❌ Отменено"
    }
    txt = f"🎫 Бронь #{b['id']}\n"
    if show_session:
        txt += f"🎬 {b.get('movie', '')} {b.get('date', '')} {b.get('time', '')}\n"
    txt += (
        f"👤 {b['first_name']} {b['last_name']}\n"
        f"📱 {b['phone']}\n"
        f"💺 Места: {b['seats']}\n"
        f"💰 {b['total_price']} ₽\n"
        f"Статус: {status_map.get(b['status'], b['status'])}"
    )
    return txt

async def safe_send_message(bot, chat_id, text, reply_markup=None):
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup
        )
        print(f"[SEND OK] chat_id={chat_id}")
        return True
    except Exception as e:
        print(f"[SEND FAIL] chat_id={chat_id} error={e}")
        return False

async def send_all_admins(text, reply_markup=None):
    bot = Bot(token=BOT_TOKEN)
    admin_ids = db.get_all_admins()
    print(f"[ADMINS] notify -> {admin_ids}")

    for aid in admin_ids:
        await safe_send_message(bot, aid, text, reply_markup=reply_markup)

# ─── BOT HANDLERS ─────────────────────────────────────────────────────────────
async def handle_message(update_data):
    bot = Bot(token=BOT_TOKEN)
    upd = Update.de_json(update_data, bot)

    print(f"[WEBHOOK UPDATE] keys={list(update_data.keys()) if isinstance(update_data, dict) else 'unknown'}")

    # Callback query
    if upd.callback_query:
        print(f"[CALLBACK] from={upd.callback_query.from_user.id} data={upd.callback_query.data}")
        await handle_callback(bot, upd.callback_query)
        return

    if not upd.message:
        print("[SKIP] no message and no callback_query")
        return

    msg = upd.message
    cid = msg.chat_id
    text = msg.text or ""
    is_admin = db.is_admin(cid)
    state = get_state(cid)

    print(f"[MESSAGE] chat_id={cid} text={text!r} is_admin={is_admin} has_web_app_data={bool(msg.web_app_data)}")

    # /start
    if text == "/start":
        clear_state(cid)
        if is_admin:
            await safe_send_message(
                bot,
                cid,
                "👋 Привет, администратор!\n\nВыбери действие:",
                reply_markup=admin_main_kb()
            )
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Открыть кассу", web_app=WebAppInfo(url=WEBAPP_URL))
            ]])
            await safe_send_message(
                bot,
                cid,
                "🎬 Понтон — кино на воде\n\nНажми кнопку, чтобы выбрать места и забронировать.",
                reply_markup=kb
            )
        return

    # Web App data
    if msg.web_app_data:
        await handle_webapp_data(bot, msg)
        return

    # Admin state machine
    if is_admin and state["step"]:
        await handle_admin_input(bot, cid, text, state)
        return

    # Default
    if not is_admin:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Открыть кассу", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
        await safe_send_message(bot, cid, "Нажми кнопку, чтобы открыть кассу:", reply_markup=kb)

async def handle_webapp_data(bot, msg):
    cid = msg.chat_id
    try:
        data = json.loads(msg.web_app_data.data)
        print(f"[WEBAPP DATA] chat_id={cid} data={data}")

        sid = data["session_id"]
        seats = data["seats"]
        first_name = data["first_name"]
        last_name = data["last_name"]
        phone = data["phone"]
        tg_id = data.get("telegram_id", cid)
        username = data.get("username", "")

        session = db.get_session(sid)
        if not session:
            await safe_send_message(bot, cid, "❌ Сеанс не найден.")
            return

        price = session["price"] * len(seats)
        bid = db.create_booking(sid, tg_id, username, first_name, last_name, phone, seats, price)

        if bid is None:
            await safe_send_message(bot, cid, "❌ Одно из выбранных мест уже занято. Попробуйте снова.")
            return

        seats_str = ", ".join(str(s) for s in seats)

        # Notify client
        await safe_send_message(
            bot,
            cid,
            f"✅ Бронь создана!\n\n"
            f"🎬 {session['movie']}\n"
            f"📅 {session['date']} в {session['time']}\n"
            f"💺 Места: {seats_str}\n"
            f"💰 Сумма: {price} ₽\n"
            f"⏳ Статус: ожидает подтверждения оплаты\n\n"
            f"Мы свяжемся с вами для подтверждения."
        )

        # Notify admins
        admin_text = (
            f"🔔 Новая бронь #{bid}\n\n"
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
        print(f"[WEBAPP ERROR] chat_id={cid} error={e}")
        await safe_send_message(bot, cid, "❌ Ошибка при создании брони.")

async def handle_callback(bot, cb):
    cid = cb.message.chat_id
    data = cb.data
    msg_id = cb.message.message_id

    try:
        await cb.answer()
    except Exception as e:
        print(f"[CALLBACK ANSWER ERROR] {e}")

    if not db.is_admin(cid):
        await safe_send_message(bot, cid, "❌ Эта команда доступна только администратору.")
        return

    # ── Admin main ──
    if data == "admin_main":
        await bot.edit_message_text(
            "Выбери действие:",
            cid,
            msg_id,
            reply_markup=admin_main_kb()
        )

    elif data == "add_session":
        set_state(cid, "add_movie", {})
        await bot.edit_message_text(
            "🎬 Введите название фильма:\n\n(или нажмите /cancel для отмены)",
            cid,
            msg_id,
            reply_markup=back_kb("admin_main")
        )

    elif data == "list_sessions":
        sessions = db.get_sessions()
        if not sessions:
            await bot.edit_message_text(
                "Сеансов нет. Добавьте первый!",
                cid,
                msg_id,
                reply_markup=back_kb("admin_main")
            )
            return

        buttons = []
        for s in sessions:
            icon = "✅" if s["is_active"] else "❌"
            buttons.append([InlineKeyboardButton(
                f"{icon} {s['movie']} — {s['date']} {s['time']}",
                callback_data=f"manage_session_{s['id']}"
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_main")])

        await bot.edit_message_text(
            "📋 Все сеансы:",
            cid,
            msg_id,
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("manage_session_"):
        sid = int(data.split("_")[-1])
        s = db.get_session(sid)
        if not s:
            await bot.edit_message_text("Сеанс не найден.", cid, msg_id)
            return

        await bot.edit_message_text(
            session_text(s),
            cid,
            msg_id,
            reply_markup=session_manage_kb(sid, s["is_active"])
        )

    elif data.startswith("toggle_"):
        sid = int(data.split("_")[-1])
        s = db.get_session(sid)
        if not s:
            await bot.edit_message_text("Сеанс не найден.", cid, msg_id)
            return

        new_val = 0 if s["is_active"] else 1
        db.update_session(sid, "is_active", new_val)
        s = db.get_session(sid)

        await bot.edit_message_text(
            session_text(s),
            cid,
            msg_id,
            reply_markup=session_manage_kb(sid, s["is_active"])
        )

    elif data.startswith("edit_"):
        parts = data.split("_")
        field = parts[1]
        sid = int(parts[2])

        prompts = {
            "movie": "Введите новое название фильма:",
            "date": "Введите новую дату (формат: 2026-03-15):",
            "time": "Введите новое время (формат: 20:00):",
            "price": "Введите новую цену (число, рублей):",
            "seats": "Введите новое количество мест:",
        }

        set_state(cid, f"edit_{field}", {"sid": sid, "msg_id": msg_id})
        await bot.edit_message_text(
            prompts.get(field, "Введите значение:"),
            cid,
            msg_id,
            reply_markup=back_kb(f"manage_session_{sid}")
        )

    elif data.startswith("delete_confirm_"):
        sid = int(data.split("_")[-1])
        await bot.edit_message_text(
            "⚠️ Вы уверены, что хотите удалить сеанс? Все брони тоже будут удалены.",
            cid,
            msg_id,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"delete_yes_{sid}")],
                [InlineKeyboardButton("◀️ Нет, назад", callback_data=f"manage_session_{sid}")]
            ])
        )

    elif data.startswith("delete_yes_"):
        sid = int(data.split("_")[-1])
        db.delete_session(sid)
        await bot.edit_message_text(
            "✅ Сеанс удалён.",
            cid,
            msg_id,
            reply_markup=back_kb("list_sessions")
        )

    elif data == "all_bookings":
        bookings = db.get_all_bookings()
        if not bookings:
            await bot.edit_message_text(
                "Броней нет.",
                cid,
                msg_id,
                reply_markup=back_kb("admin_main")
            )
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

        await bot.edit_message_text(
            "🎫 Все брони:",
            cid,
            msg_id,
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("session_bookings_"):
        sid = int(data.split("_")[-1])
        bookings = db.get_bookings_for_session(sid)
        s = db.get_session(sid)

        if not s:
            await bot.edit_message_text("Сеанс не найден.", cid, msg_id)
            return

        if not bookings:
            await bot.edit_message_text(
                f"На сеанс «{s['movie']}» броней нет.",
                cid,
                msg_id,
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
            f"Брони на «{s['movie']}»:",
            cid,
            msg_id,
            reply_markup=InlineKeyboardMarkup(buttons)
        )

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
            booking_text(b),
            cid,
            msg_id,
            reply_markup=booking_manage_kb(bid)
        )

    elif data.startswith("confirm_pay_"):
        bid = int(data.split("_")[-1])
        b = db.get_booking(bid)
        if not b:
            await cb.answer("Бронь не найдена", show_alert=True)
            return

        db.update_booking_status(bid, "paid")
        s = db.get_session(b["session_id"])

        if b["telegram_id"]:
            client_text = (
                f"🎉 Оплата подтверждена!\n\n"
                f"🎬 {s['movie']}\n"
                f"📅 {s['date']} в {s['time']}\n"
                f"💺 Места: {b['seats']}\n"
                f"💰 {b['total_price']} ₽\n"
                f"✅ Статус: Куплено\n\n"
                f"Ждём вас на Понтоне! 🌊"
            )
            await safe_send_message(bot, b["telegram_id"], client_text)

        await bot.edit_message_text(
            f"✅ Оплата подтверждена для брони #{bid}\n{b['first_name']} {b['last_name']}",
            cid,
            msg_id,
            reply_markup=back_kb("all_bookings")
        )

    elif data.startswith("cancel_booking_"):
        bid = int(data.split("_")[-1])
        b = db.get_booking(bid)
        if not b:
            await cb.answer("Бронь не найдена", show_alert=True)
            return

        db.update_booking_status(bid, "cancelled")
        s = db.get_session(b["session_id"])

        if b["telegram_id"]:
            client_text = (
                f"❌ Бронь отменена\n\n"
                f"🎬 {s['movie']}\n"
                f"📅 {s['date']} в {s['time']}\n"
                f"💺 Места: {b['seats']}\n\n"
                f"По вопросам: wa.me/79996422291"
            )
            await safe_send_message(bot, b["telegram_id"], client_text)

        await bot.edit_message_text(
            f"❌ Бронь #{bid} отменена.",
            cid,
            msg_id,
            reply_markup=back_kb("all_bookings")
        )

async def handle_admin_input(bot, cid, text, state):
    step = state["step"]
    data = state["data"]

    if text == "/cancel":
        clear_state(cid)
        await safe_send_message(bot, cid, "Отменено.", reply_markup=admin_main_kb())
        return

    if step == "add_movie":
        set_state(cid, "add_date", {"movie": text})
        await safe_send_message(bot, cid, "📅 Дата сеанса (формат: 2026-03-15):")

    elif step == "add_date":
        data["date"] = text
        set_state(cid, "add_time", data)
        await safe_send_message(bot, cid, "🕐 Время сеанса (формат: 20:00):")

    elif step == "add_time":
        data["time"] = text
        set_state(cid, "add_price", data)
        await safe_send_message(bot, cid, "💰 Цена билета (только число, рублей):")

    elif step == "add_price":
        try:
            data["price"] = int(text)
        except Exception:
            await safe_send_message(bot, cid, "❌ Введите число.")
            return

        set_state(cid, "add_seats", data)
        await safe_send_message(bot, cid, "💺 Количество мест:")

    elif step == "add_seats":
        try:
            data["seats"] = int(text)
        except Exception:
            await safe_send_message(bot, cid, "❌ Введите число.")
            return

        db.create_session(
            data["movie"],
            data["date"],
            data["time"],
            data["price"],
            data["seats"]
        )
        clear_state(cid)

        await safe_send_message(
            bot,
            cid,
            f"✅ Сеанс добавлен!\n\n"
            f"🎬 {data['movie']}\n"
            f"📅 {data['date']} в {data['time']}\n"
            f"💰 {data['price']} ₽\n"
            f"💺 {data['seats']} мест",
            reply_markup=admin_main_kb()
        )

    elif step.startswith("edit_"):
        field = step.replace("edit_", "")
        sid = data["sid"]
        val = text

        if field in ["price", "seats"]:
            try:
                val = int(text)
            except Exception:
                await safe_send_message(bot, cid, "❌ Введите число.")
                return

        field_map = {
            "movie": "movie",
            "date": "date",
            "time": "time",
            "price": "price",
            "seats": "total_seats"
        }

        db.update_session(sid, field_map.get(field, field), val)
        clear_state(cid)
        s = db.get_session(sid)

        await safe_send_message(
            bot,
            cid,
            f"✅ Обновлено!\n\n{session_text(s)}",
            reply_markup=session_manage_kb(sid, s["is_active"])
        )

# ─── SCHEDULER ────────────────────────────────────────────────────────────────
def run_scheduler():
    while True:
        try:
            sessions = db.get_sessions_starting_in(55, 65)
            for s in sessions:
                asyncio.run(send_session_summary(s))
        except Exception as e:
            print(f"[SCHEDULER ERROR] {e}")
        time_module.sleep(60)

async def send_session_summary(s):
    bookings = db.get_bookings_for_session(s["id"])
    booked_map = {}
    for b in bookings:
        if b["status"] == "cancelled":
            continue
        for seat in b["seats"].split(","):
            booked_map[int(seat.strip())] = b

    lines = [f"🎬 {s['movie']}\n📅 {s['date']} в {s['time']}\n\n📋 Список мест:\n"]
    for i in range(1, s["total_seats"] + 1):
        if i in booked_map:
            b = booked_map[i]
            lines.append(f"💺 Место {i} — {b['first_name']} {b['last_name']} — {b['phone']} — {b['status']}")
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
    try:
        update_json = request.get_json(force=True, silent=False)
        asyncio.run(handle_message(update_json))
        return "ok", 200
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
        return "error", 500

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
    try:
        data = request.get_json(force=True, silent=False)
        print(f"[API BOOK] incoming={data}")

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

        # Notify user if telegram_id exists
        if tg_id:
            user_text = (
                f"✅ Бронь создана!\n\n"
                f"🎬 {session['movie']}\n"
                f"📅 {session['date']} в {session['time']}\n"
                f"💺 Места: {seats_str}\n"
                f"💰 Сумма: {price} ₽\n"
                f"⏳ Статус: ожидает подтверждения оплаты\n\n"
                f"Мы свяжемся с вами для подтверждения."
            )
            asyncio.run(safe_send_message(Bot(token=BOT_TOKEN), tg_id, user_text))
        else:
            print("[API BOOK] telegram_id is empty, user notification skipped")

        admin_text = (
            f"🔔 Новая бронь #{bid}\n\n"
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

        asyncio.run(send_all_admins(admin_text, kb))

        return jsonify({"ok": True, "booking_id": bid, "total": price})

    except Exception as e:
        print(f"[API BOOK ERROR] {e}")
        return jsonify({"ok": False, "error": "Внутренняя ошибка сервера"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

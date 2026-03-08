import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "ponton.db")

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db

def parse_admin_ids():
    raw = os.environ.get("ADMIN_IDS", "").strip()
    ids = []

    if raw:
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))

    # запасной вариант, если переменная не задана
    if not ids:
        ids = [319637013, 327659980]

    return ids

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER UNIQUE NOT NULL,
            name TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movie TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            price INTEGER NOT NULL,
            total_seats INTEGER NOT NULL DEFAULT 30,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            telegram_id INTEGER,
            username TEXT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            seats TEXT NOT NULL,
            total_price INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS booked_seats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            seat_num INTEGER NOT NULL,
            booking_id INTEGER NOT NULL,
            UNIQUE(session_id, seat_num),
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (booking_id) REFERENCES bookings(id)
        );
    """)
    db.commit()

    admin_ids = parse_admin_ids()
    print(f"[DB] ADMIN_IDS = {admin_ids}")

    for i, tid in enumerate(admin_ids, start=1):
        try:
            db.execute(
                "INSERT OR IGNORE INTO admins (telegram_id, name) VALUES (?, ?)",
                (tid, f"Admin {i}")
            )
        except Exception as e:
            print(f"[DB] cannot insert admin {tid}: {e}")

    db.commit()
    db.close()

def is_admin(telegram_id):
    db = get_db()
    r = db.execute("SELECT id FROM admins WHERE telegram_id=?", (telegram_id,)).fetchone()
    db.close()
    return r is not None

def get_all_admins():
    db = get_db()
    r = db.execute("SELECT telegram_id FROM admins ORDER BY id").fetchall()
    db.close()
    return [row["telegram_id"] for row in r]

def create_session(movie, date, time, price, total_seats):
    db = get_db()
    cur = db.execute(
        "INSERT INTO sessions (movie, date, time, price, total_seats) VALUES (?,?,?,?,?)",
        (movie, date, time, price, total_seats)
    )
    db.commit()
    sid = cur.lastrowid
    db.close()
    return sid

def get_sessions(active_only=False):
    db = get_db()
    q = "SELECT * FROM sessions"
    if active_only:
        q += " WHERE is_active=1"
    q += " ORDER BY date, time"
    r = db.execute(q).fetchall()
    db.close()
    return [dict(row) for row in r]

def get_session(sid):
    db = get_db()
    r = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    db.close()
    return dict(r) if r else None

def update_session(sid, field, value):
    allowed = ["movie", "date", "time", "price", "total_seats", "is_active"]
    if field not in allowed:
        return False
    db = get_db()
    db.execute(f"UPDATE sessions SET {field}=? WHERE id=?", (value, sid))
    db.commit()
    db.close()
    return True

def delete_session(sid):
    db = get_db()
    db.execute("DELETE FROM booked_seats WHERE session_id=?", (sid,))
    db.execute("DELETE FROM bookings WHERE session_id=?", (sid,))
    db.execute("DELETE FROM sessions WHERE id=?", (sid,))
    db.commit()
    db.close()

def get_taken_seats(session_id):
    db = get_db()
    r = db.execute(
        "SELECT seat_num FROM booked_seats WHERE session_id=?", (session_id,)
    ).fetchall()
    db.close()
    return [row["seat_num"] for row in r]

def create_booking(session_id, telegram_id, username, first_name, last_name, phone, seats, price):
    db = get_db()
    seats_str = ",".join(str(s) for s in seats)
    try:
        cur = db.execute(
            """INSERT INTO bookings 
               (session_id, telegram_id, username, first_name, last_name, phone, seats, total_price, status)
               VALUES (?,?,?,?,?,?,?,?,'pending')""",
            (session_id, telegram_id, username, first_name, last_name, phone, seats_str, price)
        )
        booking_id = cur.lastrowid

        for seat in seats:
            db.execute(
                "INSERT INTO booked_seats (session_id, seat_num, booking_id) VALUES (?,?,?)",
                (session_id, seat, booking_id)
            )

        db.commit()
        db.close()
        return booking_id
    except sqlite3.IntegrityError:
        db.close()
        return None

def get_booking(bid):
    db = get_db()
    r = db.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
    db.close()
    return dict(r) if r else None

def get_bookings_for_session(session_id):
    db = get_db()
    r = db.execute(
        "SELECT * FROM bookings WHERE session_id=? AND status != 'cancelled' ORDER BY created_at",
        (session_id,)
    ).fetchall()
    db.close()
    return [dict(row) for row in r]

def update_booking_status(bid, status):
    db = get_db()
    db.execute("UPDATE bookings SET status=? WHERE id=?", (status, bid))
    if status == "cancelled":
        booking = db.execute("SELECT session_id, seats FROM bookings WHERE id=?", (bid,)).fetchone()
        if booking:
            for seat in booking["seats"].split(","):
                db.execute(
                    "DELETE FROM booked_seats WHERE session_id=? AND seat_num=?",
                    (booking["session_id"], int(seat))
                )
    db.commit()
    db.close()

def get_all_bookings():
    db = get_db()
    r = db.execute("""
        SELECT b.*, s.movie, s.date, s.time
        FROM bookings b
        JOIN sessions s ON b.session_id = s.id
        ORDER BY b.created_at DESC
        LIMIT 50
    """).fetchall()
    db.close()
    return [dict(row) for row in r]

def get_sessions_starting_in(minutes_from=55, minutes_to=65):
    db = get_db()
    r = db.execute("""
        SELECT * FROM sessions
        WHERE is_active=1
        AND datetime(date || ' ' || time) BETWEEN
            datetime('now', 'localtime', '+' || ? || ' minutes') AND
            datetime('now', 'localtime', '+' || ? || ' minutes')
    """, (minutes_from, minutes_to)).fetchall()
    db.close()
    return [dict(row) for row in r]

import logging
import sqlite3
from datetime import date, timedelta, time, datetime
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import csv
import tempfile

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

ADD_NAME, ADD_SEX, ADD_WEIGHT, ADD_CAGE = range(4)



# ================== CONFIG ==================
# Get token from environment (Render Environment -> BOT_TOKEN)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

# ================== ADD RABBIT WIZARD STATES ==================

(
    ADD_NAME,
    ADD_SEX,
    ADD_CAGE,
    ADD_SECTION,
    ADD_WEIGHT,
) = range(5)


# OWNER_ID:
#  - 0 means "no owner set yet" -> everyone can use commands
#  - Once you know your Telegram user ID from /whoami,
#    replace 0 with your ID (e.g. OWNER_ID = 123456789) to make bot private.
OWNER_ID = 5891168987 # <<< CHANGE THIS to your Telegram user ID to make the bot private

DB_FILE = "rabbits.db"

GESTATION_DAYS = 31
WEANING_DAYS = 35


# ================== DB HELPERS ==================

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def safe_alter(cur, sql):
    try:
        cur.execute(sql)
    except sqlite3.OperationalError:
        pass


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Rabbits
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rabbits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            sex TEXT CHECK(sex IN ('M','F')) NOT NULL
        )
    """)
    safe_alter(cur, "ALTER TABLE rabbits ADD COLUMN mother_id INTEGER")
    safe_alter(cur, "ALTER TABLE rabbits ADD COLUMN father_id INTEGER")
    safe_alter(cur, "ALTER TABLE rabbits ADD COLUMN cage TEXT")
    safe_alter(cur, "ALTER TABLE rabbits ADD COLUMN section TEXT")
    safe_alter(cur, "ALTER TABLE rabbits ADD COLUMN status TEXT DEFAULT 'active'")
    safe_alter(cur, "ALTER TABLE rabbits ADD COLUMN death_date TEXT")
    safe_alter(cur, "ALTER TABLE rabbits ADD COLUMN death_reason TEXT")
    safe_alter(cur, "ALTER TABLE rabbits ADD COLUMN photo_file_id TEXT")

    # Breedings
    cur.execute("""
        CREATE TABLE IF NOT EXISTS breedings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doe_id INTEGER NOT NULL,
            buck_id INTEGER NOT NULL,
            mating_date TEXT NOT NULL,
            expected_due_date TEXT NOT NULL,
            kindling_date TEXT,
            litter_size INTEGER,
            weaning_date TEXT,
            litter_name TEXT,
            FOREIGN KEY (doe_id) REFERENCES rabbits(id),
            FOREIGN KEY (buck_id) REFERENCES rabbits(id)
        )
    """)

    # Health records
    cur.execute("""
        CREATE TABLE IF NOT EXISTS health_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rabbit_id INTEGER NOT NULL,
            record_date TEXT NOT NULL,
            note TEXT NOT NULL,
            FOREIGN KEY (rabbit_id) REFERENCES rabbits(id)
        )
    """)

    # Sales
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rabbit_id INTEGER NOT NULL,
            sale_date TEXT NOT NULL,
            price REAL,
            buyer TEXT,
            FOREIGN KEY (rabbit_id) REFERENCES rabbits(id)
        )
    """)

    # Expenses
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exp_date TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT
        )
    """)

    # Feed logs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feed_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT NOT NULL,
            amount_kg REAL NOT NULL,
            cost REAL,
            note TEXT
        )
    """)

    # Weights
    cur.execute("""
        CREATE TABLE IF NOT EXISTS weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rabbit_id INTEGER NOT NULL,
            weigh_date TEXT NOT NULL,
            weight_kg REAL NOT NULL,
            FOREIGN KEY (rabbit_id) REFERENCES rabbits(id)
        )
    """)

    # Tasks
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_date TEXT NOT NULL,
            title TEXT NOT NULL,
            note TEXT,
            done INTEGER DEFAULT 0
        )
    """)

    # Settings (for climate, etc.)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Achievements (gamification)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            unlocked_date TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def set_setting(key: str, value: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO settings(key, value)
        VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    conn.commit()
    conn.close()


def get_setting(key: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else None


# ================== OWNER CHECK (PRIVACY) ==================

def is_owner(update: Update) -> bool:
    """
    Returns True if:
      - OWNER_ID == 0 (no owner set yet, open mode)
      - OR caller's user.id == OWNER_ID
    """
    user = update.effective_user
    if OWNER_ID == 0:
        # no owner set yet -> allow everyone
        return True
    return user is not None and user.id == OWNER_ID


async def ensure_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if caller is owner, else sends error and returns False."""
    if not is_owner(update):
        if update.message:
            await update.message.reply_text(
                "⛔ This bot is private. You are not allowed to use this command."
            )
        return False
    return True


# ================== BASIC RABBIT FUNCS ==================

def add_rabbit(name, sex):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO rabbits(name, sex) VALUES (?, ?)", (name, sex))
        conn.commit()

        # === Achievements: rabbit counts ===
        cur.execute("SELECT COUNT(*) AS c FROM rabbits")
        total = cur.fetchone()["c"]
        if total == 1:
            unlock_achievement("first_rabbit")
        if total >= 10:
            unlock_achievement("ten_rabbits")
        if total >= 50:
            unlock_achievement("fifty_rabbits")

        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()



def list_rabbits(active_only=False):
    conn = get_db()
    cur = conn.cursor()
    if active_only:
        cur.execute("SELECT * FROM rabbits WHERE status='active' ORDER BY name")
    else:
        cur.execute("SELECT * FROM rabbits ORDER BY name")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_rabbit(name):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rabbits WHERE name = ?", (name,))
    row = cur.fetchone()
    conn.close()
    return row


def get_rabbit_by_id(rid):
    if rid is None:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rabbits WHERE id = ?", (rid,))
    row = cur.fetchone()
    conn.close()
    return row


def update_rabbit_parents(child_name, mother_name, father_name):
    child = get_rabbit(child_name)
    mother = get_rabbit(mother_name)
    father = get_rabbit(father_name)
    if not child:
        return "❌ Child not found."
    if not mother or not father:
        return "❌ Mother or father not found."
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE rabbits SET mother_id=?, father_id=? WHERE id=?
    """, (mother["id"], father["id"], child["id"]))
    conn.commit()
    conn.close()
    return f"✅ Parents set for {child_name}: mother {mother_name}, father {father_name}."


def set_cage_section(name, cage, section=None):
    r = get_rabbit(name)
    if not r:
        return "❌ Rabbit not found."
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE rabbits SET cage=?, section=? WHERE id=?
    """, (cage, section, r["id"]))
    conn.commit()
    conn.close()
    msg = f"✅ {name} assigned to cage {cage}"
    if section:
        msg += f", section {section}"
    return msg + "."


def mark_dead(name, reason=None):
    r = get_rabbit(name)
    if not r:
        return "❌ Rabbit not found."
    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE rabbits SET status='dead', death_date=?, death_reason=? WHERE id=?
    """, (today_str, reason, r["id"]))
    conn.commit()
    conn.close()
    return f"☠️ {name} marked as dead." + (f" Reason: {reason}" if reason else "")


# ==== INBREEDING ASSESSMENT ====

def assess_inbreeding(name1, name2):
    """
    Returns (severity, message)
    severity in {"error", "danger", "warning", "none"}.
    """
    r1 = get_rabbit(name1)
    r2 = get_rabbit(name2)
    if not r1 or not r2:
        return "error", "❌ One or both rabbits not found."
    if r1["id"] == r2["id"]:
        return "error", "❌ Same rabbit."

    parents1 = set(x for x in [r1["mother_id"], r1["father_id"]] if x)
    parents2 = set(x for x in [r2["mother_id"], r2["father_id"]] if x)

    # Parent–offspring
    if r1["id"] in parents2 or r2["id"] in parents1:
        return "danger", "⚠️ DANGEROUS inbreeding: parent–offspring."

    # Shared parents = siblings (full or half)
    common_parents = parents1 & parents2
    if common_parents:
        full = (
            r1["mother_id"]
            and r1["mother_id"] == r2["mother_id"]
            and r1["father_id"]
            and r1["father_id"] == r2["father_id"]
        )
        parent_names = []
        for pid in common_parents:
            p = get_rabbit_by_id(pid)
            if p:
                parent_names.append(p["name"])
        parents_str = ", ".join(parent_names) if parent_names else "shared parent"

        if full:
            msg = f"⚠️ DANGEROUS inbreeding: full siblings (parents: {parents_str})."
        else:
            msg = f"⚠️ DANGEROUS inbreeding: half-siblings (shared parent(s): {parents_str})."
        return "danger", msg

    # Grandparents (cousin-level)
    def grandparents_ids(r):
        ids = set()
        for pid in [r["mother_id"], r["father_id"]]:
            if pid:
                pr = get_rabbit_by_id(pid)
                if pr:
                    for g in [pr["mother_id"], pr["father_id"]]:
                        if g:
                            ids.add(g)
        return ids

    gp1 = grandparents_ids(r1)
    gp2 = grandparents_ids(r2)
    common_gp = gp1 & gp2
    if common_gp:
        names = []
        for gid in common_gp:
            g = get_rabbit_by_id(gid)
            if g:
                names.append(g["name"])
        if names:
            return "warning", f"⚠️ Related: shared grandparent(s) {', '.join(names)}."
        else:
            return "warning", "⚠️ Related: shared grandparent(s)."

    return "none", "✅ No close relation found (parents/grandparents)."


def checkpair_inbreeding(name1, name2):
    """Keeps old interface for /checkpair, just returns the message."""
    _, msg = assess_inbreeding(name1, name2)
    return msg


# ==== PHOTO SUPPORT ====

def set_rabbit_photo(name: str, file_id: str):
    """Save Telegram file_id of a photo for a rabbit."""
    r = get_rabbit(name)
    if not r:
        return False, "❌ Rabbit not found. Make sure the caption matches the rabbit's name."
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE rabbits SET photo_file_id=? WHERE id=?", (file_id, r["id"]))
    conn.commit()
    conn.close()
    return True, f"✅ Photo saved for {name}."


# ================== BREEDING & LITTERS ==================

def add_breeding(doe_name, buck_name):
    doe = get_rabbit(doe_name)
    buck = get_rabbit(buck_name)
    if not doe or not buck:
        return "❌ Rabbit not found."
    if doe["sex"] != "F" or buck["sex"] != "M":
        return "❌ Sex mismatch (doe must be F, buck must be M)."

    mating = date.today()
    due = mating + timedelta(days=GESTATION_DAYS)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO breedings(doe_id, buck_id, mating_date, expected_due_date)
        VALUES (?, ?, ?, ?)
    """, (doe["id"], buck["id"],
          mating.strftime("%Y-%m-%d"),
          due.strftime("%Y-%m-%d")))
    conn.commit()
    conn.close()

    return f"✅ {doe_name} bred with {buck_name}\nDue: {due}"


def record_kindling(doe_name, litter_size, litter_name=None):
    doe = get_rabbit(doe_name)
    if not doe:
        return "❌ Doe not found."

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM breedings
        WHERE doe_id=? AND kindling_date IS NULL
        ORDER BY DATE(mating_date) DESC
        LIMIT 1
    """, (doe["id"],))
    breeding = cur.fetchone()
    if not breeding:
        conn.close()
        return "❌ No open breeding found for this doe."

    kindling = date.today()
    weaning = kindling + timedelta(days=WEANING_DAYS)

    if litter_name:
        cur.execute("""
            UPDATE breedings
            SET kindling_date=?, litter_size=?, weaning_date=?, litter_name=?
            WHERE id=?
        """, (kindling.strftime("%Y-%m-%d"),
              litter_size,
              weaning.strftime("%Y-%m-%d"),
              litter_name,
              breeding["id"]))
    else:
        cur.execute("""
            UPDATE breedings
            SET kindling_date=?, litter_size=?, weaning_date=?
            WHERE id=?
        """, (kindling.strftime("%Y-%m-%d"),
              litter_size,
              weaning.strftime("%Y-%m-%d"),
              breeding["id"]))
    conn.commit()
    conn.close()

    # === Achievements: litters & kits ===
    conn2 = get_db()
    cur2 = conn2.cursor()
    cur2.execute("""
        SELECT COUNT(*) AS c FROM breedings
        WHERE kindling_date IS NOT NULL
    """)
    litters = cur2.fetchone()["c"]
    if litters == 1:
        unlock_achievement("first_litter")

    cur2.execute("""
        SELECT COALESCE(SUM(litter_size), 0) AS s
        FROM breedings
        WHERE litter_size IS NOT NULL
    """)
    kits = cur2.fetchone()["s"]
    if kits >= 50:
        unlock_achievement("fifty_kits")
    if kits >= 200:
        unlock_achievement("two_hundred_kits")
    conn2.close()

    msg = f"🍼 Kindling recorded for {doe_name}\nLitter size: {litter_size}\nWeaning: {weaning}"
    if litter_name:
        msg += f"\nLitter name: {litter_name}"
    return msg



def get_due_today():
    today = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.name
        FROM breedings b
        JOIN rabbits r ON r.id=b.doe_id
        WHERE b.expected_due_date=?
    """, (today,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_weaning_today():
    today = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.name
        FROM breedings b
        JOIN rabbits r ON r.id=b.doe_id
        WHERE b.weaning_date=?
    """, (today,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_litters_for_doe(doe_name):
    doe = get_rabbit(doe_name)
    if not doe:
        return None, []

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            b.mating_date,
            b.kindling_date,
            b.litter_size,
            b.litter_name,
            rbuck.name AS buck_name
        FROM breedings b
        JOIN rabbits rbuck ON rbuck.id = b.buck_id
        WHERE b.doe_id = ? AND b.kindling_date IS NOT NULL
        ORDER BY DATE(b.kindling_date) DESC, DATE(b.mating_date) DESC
    """, (doe["id"],))
    rows = cur.fetchall()
    conn.close()
    return doe, rows


def set_litter_name_for_latest(doe_name, litter_name):
    doe = get_rabbit(doe_name)
    if not doe:
        return "❌ Doe not found."

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM breedings
        WHERE doe_id = ? AND kindling_date IS NOT NULL
        ORDER BY DATE(kindling_date) DESC, DATE(mating_date) DESC
        LIMIT 1
    """, (doe["id"],))
    row = cur.fetchone()
    if not row:
        conn.close()
        return "❌ No litters found for this doe."

    cur.execute("UPDATE breedings SET litter_name=? WHERE id=?", (litter_name, row["id"]))
    conn.commit()
    conn.close()
    return f"✅ Litter name set to '{litter_name}' for {doe_name}."


def get_next_due_for_doe(doe_name):
    doe = get_rabbit(doe_name)
    if not doe:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM breedings
        WHERE doe_id=? AND kindling_date IS NULL
        ORDER BY DATE(expected_due_date) ASC
        LIMIT 1
    """, (doe["id"],))
    row = cur.fetchone()
    conn.close()
    return row


# ================== HEALTH, WEIGHTS, SALES ==================

def add_health_record(name, note):
    rabbit = get_rabbit(name)
    if not rabbit:
        return "❌ Rabbit not found."
    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO health_records(rabbit_id, record_date, note)
        VALUES (?, ?, ?)
    """, (rabbit["id"], today_str, note))
    conn.commit()
    conn.close()
    return f"✅ Health note added for {name}."


def get_health_log(name, limit=5):
    rabbit = get_rabbit(name)
    if not rabbit:
        return None, []
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT record_date, note
        FROM health_records
        WHERE rabbit_id = ?
        ORDER BY record_date DESC, id DESC
        LIMIT ?
    """, (rabbit["id"], limit))
    rows = cur.fetchall()
    conn.close()
    return rabbit, rows


def record_sale(name, price, buyer):
    rabbit = get_rabbit(name)
    if not rabbit:
        return "❌ Rabbit not found."

    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sales(rabbit_id, sale_date, price, buyer)
        VALUES (?, ?, ?, ?)
    """, (rabbit["id"], today_str, price, buyer))
    cur.execute("UPDATE rabbits SET status='sold' WHERE id=?", (rabbit["id"],))
    conn.commit()
    conn.close()

    # === Achievements: sales & profit ===
    unlock_achievement("first_sale")
    income, expenses, profit = get_profit_summary(None)
    if profit > 0:
        unlock_achievement("profit_positive")

    extra = ""
    if price is not None:
        extra += f" for {price}"
    if buyer:
        extra += f" to {buyer}"
    return f"💸 Sale recorded for {name}{extra}."



def add_weight(name, weight_kg):
    rabbit = get_rabbit(name)
    if not rabbit:
        return "❌ Rabbit not found."
    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO weights(rabbit_id, weigh_date, weight_kg)
        VALUES (?, ?, ?)
    """, (rabbit["id"], today_str, weight_kg))
    conn.commit()
    conn.close()
    return f"✅ Recorded weight {weight_kg} kg for {name}."


def get_weight_log(name, limit=5):
    rabbit = get_rabbit(name)
    if not rabbit:
        return None, []
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT weigh_date, weight_kg
        FROM weights
        WHERE rabbit_id = ?
        ORDER BY weigh_date DESC, id DESC
        LIMIT ?
    """, (rabbit["id"], limit))
    rows = cur.fetchall()
    conn.close()
    return rabbit, rows


# ================== EXPENSES, FEED, PROFIT ==================

def add_expense(amount, category, note=None):
    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO expenses(exp_date, category, amount, note)
        VALUES (?, ?, ?, ?)
    """, (today_str, category, amount, note))
    conn.commit()
    conn.close()
    return f"✅ Expense recorded: {amount} ({category})."


def add_feed(amount_kg, cost, note=None):
    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO feed_logs(log_date, amount_kg, cost, note)
        VALUES (?, ?, ?, ?)
    """, (today_str, amount_kg, cost, note))
    conn.commit()
    conn.close()
    return f"✅ Feed log: {amount_kg} kg, cost {cost}."


def get_profit_summary(period=None):
    conn = get_db()
    cur = conn.cursor()

    sales_where = ""
    exp_where = ""
    params_sales = []
    params_exp = []

    if period is None:
        pass
    elif len(period) == 7 and period[4] == "-":  # YYYY-MM
        sales_where = "WHERE sale_date LIKE ?"
        exp_where = "WHERE exp_date LIKE ?"
        like = period + "%"
        params_sales = [like]
        params_exp = [like]
    elif len(period) == 4 and period.isdigit():  # YYYY
        sales_where = "WHERE sale_date LIKE ?"
        exp_where = "WHERE exp_date LIKE ?"
        like = period + "%"
        params_sales = [like]
        params_exp = [like]

    cur.execute(f"SELECT COALESCE(SUM(price),0) AS s FROM sales {sales_where}", params_sales)
    income = cur.fetchone()["s"]

    cur.execute(f"SELECT COALESCE(SUM(amount),0) AS e FROM expenses {exp_where}", params_exp)
    expenses = cur.fetchone()["e"]

    conn.close()
    return income, expenses, income - expenses


def get_feed_stats(period=None):
    conn = get_db()
    cur = conn.cursor()

    where = ""
    params = []

    if period is None:
        pass
    elif len(period) == 7 and period[4] == "-":  # YYYY-MM
        where = "WHERE log_date LIKE ?"
        params = [period + "%"]
    elif len(period) == 4 and period.isdigit():  # YYYY
        where = "WHERE log_date LIKE ?"
        params = [period + "%"]

    cur.execute(f"""
        SELECT COALESCE(SUM(amount_kg),0) AS kg, COALESCE(SUM(cost),0) AS c
        FROM feed_logs {where}
    """, params)
    row = cur.fetchone()
    conn.close()
    return row["kg"], row["c"]


# ================== TASKS ==================

def add_task(task_date_str, title, note=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tasks(task_date, title, note)
        VALUES (?, ?, ?)
    """, (task_date_str, title, note))
    conn.commit()
    conn.close()
    return "✅ Task added."


def get_tasks_for_date(d):
    ds = d.strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM tasks
        WHERE task_date=? AND done=0
        ORDER BY id
    """, (ds,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_upcoming_tasks(limit=10):
    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM tasks
        WHERE task_date>=? AND done=0
        ORDER BY task_date, id
        LIMIT ?
    """, (today_str, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def mark_task_done(task_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET done=1 WHERE id=?", (task_id,))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed > 0


# ================== STATS & INFO ==================

def get_stats_message():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM rabbits")
    total_rabbits = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM rabbits WHERE sex='F'")
    total_does = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM rabbits WHERE sex='M'")
    total_bucks = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM rabbits WHERE status='active'")
    active_rabbits = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM breedings")
    total_breedings = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM breedings WHERE kindling_date IS NOT NULL")
    total_litters = cur.fetchone()["c"]

    cur.execute("SELECT COALESCE(SUM(litter_size), 0) AS s FROM breedings WHERE litter_size IS NOT NULL")
    total_kits = cur.fetchone()["s"]

    cur.execute("SELECT COUNT(*) AS c FROM sales")
    total_sales = cur.fetchone()["c"]

    conn.close()

    msg = "📊 Farm stats:\n"
    msg += f"- Rabbits: {total_rabbits} (Active: {active_rabbits}, Does: {total_does}, Bucks: {total_bucks})\n"
    msg += f"- Breedings: {total_breedings}\n"
    msg += f"- Litters recorded: {total_litters}\n"
    msg += f"- Kits recorded: {int(total_kits) if total_kits is not None else 0}\n"
    msg += f"- Sales recorded: {total_sales}\n"
    return msg


def get_info_message(name):
    r = get_rabbit(name)
    if not r:
        return "❌ Rabbit not found."

    lines = [f"🐰 {r['name']} ({r['sex']})"]
    lines.append(f"Status: {r['status'] or 'unknown'}")

    if r["status"] == "dead":
        if r["death_date"]:
            lines.append(f"  Died: {r['death_date']}")
        if r["death_reason"]:
            lines.append(f"  Reason: {r['death_reason']}")

    if r["cage"] or r["section"]:
        loc = []
        if r["cage"]:
            loc.append(f"cage {r['cage']}")
        if r["section"]:
            loc.append(f"section {r['section']}")
        lines.append("Location: " + ", ".join(loc))

    mother = get_rabbit_by_id(r["mother_id"])
    father = get_rabbit_by_id(r["father_id"])
    if mother or father:
        m = mother["name"] if mother else "unknown"
        f = father["name"] if father else "unknown"
        lines.append(f"Parents: {m} × {f}")

    if r["photo_file_id"]:
        lines.append("Photo: 📷 stored (use /photo " + r["name"] + " to view)")

    if r["sex"] == "F":
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) AS c, COALESCE(SUM(litter_size),0) AS s
            FROM breedings
            WHERE doe_id=? AND kindling_date IS NOT NULL
        """, (r["id"],))
        row = cur.fetchone()
        litters = row["c"]
        kits = int(row["s"])
        cur.execute("""
            SELECT * FROM breedings
            WHERE doe_id=? AND kindling_date IS NOT NULL
            ORDER BY DATE(kindling_date) DESC
            LIMIT 1
        """, (r["id"],))
        last = cur.fetchone()
        conn.close()

        lines.append(f"Litters: {litters} (total kits: {kits})")
        if last:
            ln = last["litter_name"] or "(no name)"
            lines.append(
                f"Last litter: {ln}, kindled {last['kindling_date']}, {last['litter_size']} kits"
            )

        nxt = get_next_due_for_doe(name)
        if nxt:
            lines.append(f"Next due: {nxt['expected_due_date']} (bred on {nxt['mating_date']})")

    rabbit, h_records = get_health_log(name, limit=1)
    if rabbit and h_records:
        lines.append(f"Last health: {h_records[0]['record_date']} – {h_records[0]['note']}")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM sales
        WHERE rabbit_id=?
        ORDER BY sale_date DESC, id DESC
        LIMIT 1
    """, (r["id"],))
    s = cur.fetchone()
    conn.close()
    if s:
        lines.append(f"Last sale: {s['sale_date']} for {s['price']} to {s['buyer'] or 'unknown buyer'}")

    return "\n".join(lines)


def get_farmsummary_message():
    stats = get_stats_message()
    income_all, exp_all, prof_all = get_profit_summary(None)
    feed_kg, feed_cost = get_feed_stats(None)

    msg = stats + "\n\n💰 Financial (all time):\n"
    msg += f"- Income: {income_all}\n"
    msg += f"- Expenses: {exp_all}\n"
    msg += f"- Profit: {prof_all}\n"
    msg += "\n🌾 Feed (all time):\n"
    msg += f"- Total feed: {feed_kg} kg\n"
    msg += f"- Feed cost: {feed_cost}\n"
    return msg


# ================== ADVANCED ANALYTICS & UTILITIES ==================

def build_family_tree(name: str) -> str:
    """Return a small text family tree for a rabbit."""
    r = get_rabbit(name)
    if not r:
        return "❌ Rabbit not found."

    lines = [f"👨‍👩‍👧 Family tree for {r['name']} ({r['sex']})"]

    # Parents
    mother = get_rabbit_by_id(r["mother_id"])
    father = get_rabbit_by_id(r["father_id"])
    if mother or father:
        m = mother["name"] if mother else "unknown"
        f = father["name"] if father else "unknown"
        lines.append(f"Parents: {m} × {f}")
    else:
        lines.append("Parents: unknown")

    # Grandparents
    def parent_names(p):
        if not p:
            return "unknown"
        gm = get_rabbit_by_id(p["mother_id"])
        gf = get_rabbit_by_id(p["father_id"])
        gm_name = gm["name"] if gm else "unknown"
        gf_name = gf["name"] if gf else "unknown"
        return f"{gm_name} × {gf_name}"

    if mother or father:
        lines.append("Grandparents:")
        if mother:
            lines.append(f"  Maternal: {parent_names(mother)}")
        if father:
            lines.append(f"  Paternal: {parent_names(father)}")

    # Children (direct)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT name, sex FROM rabbits
        WHERE mother_id=? OR father_id=?
        ORDER BY name
    """, (r["id"], r["id"]))
    children = cur.fetchall()
    conn.close()

    if children:
        lines.append("Children:")
        for c in children:
            lines.append(f"  - {c['name']} ({c['sex']})")
    else:
        lines.append("Children: none recorded")

    return "\n".join(lines)


def compute_growth_message(name: str) -> str:
    """Use weight log to compute average daily gain."""
    rabbit, rows = get_weight_log(name, limit=1000)
    if not rabbit:
        return "❌ Rabbit not found."
    if len(rows) < 2:
        return f"Not enough weight records for {name} (need at least 2)."

    # rows are ordered by weigh_date DESC, so reverse
    records = list(reversed(rows))
    data = []
    for r in records:
        try:
            d = datetime.fromisoformat(r["weigh_date"]).date()
        except Exception:
            continue
        data.append((d, float(r["weight_kg"])))

    if len(data) < 2:
        return f"Not enough valid weight records for {name}."

    start_date, start_w = data[0]
    end_date, end_w = data[-1]
    days = (end_date - start_date).days
    if days <= 0:
        return f"Growth period is too short for {name}."

    gain = end_w - start_w
    daily = gain / days

    msg = [f"📈 Growth for {rabbit['name']}:"]
    msg.append(f"- From {start_date} ({start_w} kg) to {end_date} ({end_w} kg)")
    msg.append(f"- Total gain: {gain:.3f} kg over {days} days")
    msg.append(f"- Average daily gain: {daily*1000:.1f} g/day")

    if daily * 1000 < 15:
        msg.append("⚠️ Growth seems slow. Check health, feed quality and quantity.")
    elif daily * 1000 > 35:
        msg.append("✅ Very good growth rate.")
    else:
        msg.append("🙂 Normal growth rate.")

    return "\n".join(msg)


def build_growth_chart_ascii(name: str) -> str:
    """Return ASCII chart of weights over time."""
    rabbit, rows = get_weight_log(name, limit=50)
    if not rabbit:
        return "❌ Rabbit not found."
    if len(rows) < 2:
        return f"Not enough weight records for {name} (need at least 2)."

    records = list(reversed(rows))
    data = []
    for r in records:
        try:
            d = datetime.fromisoformat(r["weigh_date"]).date()
        except Exception:
            continue
        data.append((d, float(r["weight_kg"])))
    if len(data) < 2:
        return f"Not enough valid weight records for {name}."

    weights = [w for _, w in data]
    min_w = min(weights)
    max_w = max(weights)

    if max_w == min_w:
        lines = [f"📊 Growth chart for {rabbit['name']}:"]
        for d, w in data:
            lines.append(f"{d}: {w:.3f} kg | ▇")
        return "\n".join(lines)

    lines = [f"📊 Growth chart for {rabbit['name']}: (ASCII)"]
    max_blocks = 10
    for d, w in data:
        rel = (w - min_w) / (max_w - min_w)
        blocks = int(round(rel * max_blocks))
        blocks = max(1, blocks)
        bar = "▇" * blocks
        lines.append(f"{d}: {w:.3f} kg | {bar}")

    lines.append(f"\nMin: {min_w:.3f} kg, Max: {max_w:.3f} kg")
    return "\n".join(lines)


def get_growth_stats(name: str):
    """Return (has_data, daily_grams, days, gain_kg) for internal decisions."""
    rabbit, rows = get_weight_log(name, limit=1000)
    if not rabbit or len(rows) < 2:
        return False, None, None, None

    records = list(reversed(rows))
    data = []
    for r in records:
        try:
            d = datetime.fromisoformat(r["weigh_date"]).date()
        except Exception:
            continue
        data.append((d, float(r["weight_kg"])))
    if len(data) < 2:
        return False, None, None, None

    start_date, start_w = data[0]
    end_date, end_w = data[-1]
    days = (end_date - start_date).days
    if days <= 0:
        return False, None, None, None

    gain = end_w - start_w
    daily = (gain / days) * 1000.0  # g/day
    return True, daily, days, gain


def export_table_to_csv(query: str, params, headers, filename_prefix: str) -> str | None:
    """
    Run SQL and write results as CSV to a temporary file.
    Returns the full file path or None if no rows.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(query, params or [])
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return None

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename_prefix}.csv")
    tmp_path = tmp.name

    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row[h] for h in headers])

    return tmp_path


def get_backup_db_path() -> str | None:
    """Return path to rabbits.db if it exists, else None."""
    if os.path.exists(DB_FILE):
        return DB_FILE
    return None


def get_line_performance_message(name: str) -> str:
    """Basic line performance: litters, kits, survival, income from offspring."""
    r = get_rabbit(name)
    if not r:
        return "❌ Rabbit not found."

    conn = get_db()
    cur = conn.cursor()

    if r["sex"] == "F":
        # Doe line: based on her breedings and children as mother
        cur.execute("""
            SELECT COUNT(*) AS c, COALESCE(SUM(litter_size),0) AS s
            FROM breedings
            WHERE doe_id=?
        """, (r["id"],))
        br = cur.fetchone()
        litters = br["c"]
        total_kits_recorded = int(br["s"] or 0)

        cur.execute("SELECT COUNT(*) AS c FROM rabbits WHERE mother_id=?", (r["id"],))
        kits_alive = cur.fetchone()["c"]

        cur.execute("""
            SELECT COALESCE(SUM(s.price),0) AS income
            FROM rabbits k
            JOIN sales s ON s.rabbit_id = k.id
            WHERE k.mother_id=?
        """, (r["id"],))
        income = cur.fetchone()["income"]
    else:
        # Buck line: based on breedings with him and children as father
        cur.execute("""
            SELECT COUNT(*) AS c, COALESCE(SUM(litter_size),0) AS s
            FROM breedings
            WHERE buck_id=?
        """, (r["id"],))
        br = cur.fetchone()
        litters = br["c"]
        total_kits_recorded = int(br["s"] or 0)

        cur.execute("SELECT COUNT(*) AS c FROM rabbits WHERE father_id=?", (r["id"],))
        kits_alive = cur.fetchone()["c"]

        cur.execute("""
            SELECT COALESCE(SUM(s.price),0) AS income
            FROM rabbits k
            JOIN sales s ON s.rabbit_id = k.id
            WHERE k.father_id=?
        """, (r["id"],))
        income = cur.fetchone()["income"]

    conn.close()

    avg_litter = (total_kits_recorded / litters) if litters > 0 else 0
    survival_rate = (kits_alive / total_kits_recorded * 100) if total_kits_recorded > 0 else None

    lines = [f"📊 Line performance for {r['name']} ({r['sex']}):"]
    lines.append(f"- Litters recorded: {litters}")
    lines.append(f"- Total kits recorded: {total_kits_recorded}")
    lines.append(f"- Kits currently in DB from this line: {kits_alive}")
    lines.append(f"- Average litter size: {avg_litter:.2f}" if litters > 0 else "- Average litter size: n/a")
    if survival_rate is not None:
        lines.append(f"- Approx. survival (kits in DB / kits recorded): {survival_rate:.1f}%")
    else:
        lines.append("- Survival: n/a")
    lines.append(f"- Income from offspring sales: {income}")

    # Simple rating
    rating = "⭐"
    if litters >= 3 and survival_rate and survival_rate >= 85 and income >= 0:
        rating = "⭐⭐⭐⭐"
    elif litters >= 2 and survival_rate and survival_rate >= 70:
        rating = "⭐⭐⭐"
    elif litters >= 1:
        rating = "⭐⭐"

    lines.append(f"- Line rating: {rating}")
    return "\n".join(lines)


def decide_keep_or_sell(name: str) -> str:
    """Heuristic suggestion to keep as breeder or sell."""
    r = get_rabbit(name)
    if not r:
        return "❌ Rabbit not found."

    has_growth, daily_g, days, gain = get_growth_stats(name)

    conn = get_db()
    cur = conn.cursor()

    if r["sex"] == "F":
        # Doe: look at litters & survival & income
        cur.execute("""
            SELECT COUNT(*) AS c, COALESCE(SUM(litter_size),0) AS s
            FROM breedings
            WHERE doe_id=? AND kindling_date IS NOT NULL
        """, (r["id"],))
        br = cur.fetchone()
        litters = br["c"]
        total_kits_recorded = int(br["s"] or 0)

        cur.execute("SELECT COUNT(*) AS c FROM rabbits WHERE mother_id=?", (r["id"],))
        kits_alive = cur.fetchone()["c"]

        cur.execute("""
            SELECT COALESCE(SUM(s.price),0) AS income
            FROM rabbits k
            JOIN sales s ON s.rabbit_id = k.id
            WHERE k.mother_id=?
        """, (r["id"],))
        income = cur.fetchone()["income"]
    else:
        # Buck: children count and income
        cur.execute("SELECT COUNT(*) AS c FROM rabbits WHERE father_id=?", (r["id"],))
        kits_alive = cur.fetchone()["c"]

        cur.execute("""
            SELECT COALESCE(SUM(s.price),0) AS income
            FROM rabbits k
            JOIN sales s ON s.rabbit_id = k.id
            WHERE k.father_id=?
        """, (r["id"],))
        income = cur.fetchone()["income"]

        litters = None
        total_kits_recorded = None

    conn.close()

    lines = [f"🧠 Keep or sell analysis for {r['name']} ({r['sex']}):"]

    if has_growth:
        lines.append(f"- Growth: {daily_g:.1f} g/day over {days} days (total gain {gain:.3f} kg)")
    else:
        lines.append("- Growth: not enough data (add more /weight logs)")

    if r["sex"] == "F":
        lines.append(f"- Litters: {litters}, kits recorded: {total_kits_recorded}, kits alive in DB: {kits_alive}")
    else:
        lines.append(f"- Offspring currently in DB: {kits_alive}")

    lines.append(f"- Income from offspring: {income}")

    # Simple rules
    recommendation = ""

    if r["sex"] == "F":
        survival_rate = (kits_alive / total_kits_recorded * 100) if total_kits_recorded else None

        if litters and litters >= 2 and survival_rate and survival_rate >= 80 and (not has_growth or daily_g >= 20):
            recommendation = "✅ Recommendation: KEEP as breeder (good mother line)."
        elif (litters is None or litters == 0) and has_growth and daily_g < 20:
            recommendation = "❌ Recommendation: SELL / meat (no litters and slow growth)."
        else:
            recommendation = "➖ Recommendation: Borderline – keep under observation."
    else:
        if kits_alive >= 20 and (not has_growth or daily_g >= 20):
            recommendation = "✅ Recommendation: KEEP as breeding buck (many offspring & decent growth)."
        elif kits_alive == 0 and has_growth and daily_g < 20:
            recommendation = "❌ Recommendation: SELL / meat (no offspring and slow growth)."
        else:
            recommendation = "➖ Recommendation: Borderline – keep under observation."

    lines.append("")
    lines.append(recommendation)
    return "\n".join(lines)


def suggest_breeding_pairs(limit: int = 5):
    """Return a list of suggested doe-buck pairs with a score."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM rabbits WHERE sex='F' AND status='active' ORDER BY name")
    does = cur.fetchall()
    cur.execute("SELECT * FROM rabbits WHERE sex='M' AND status='active' ORDER BY name")
    bucks = cur.fetchall()

    if not does or not bucks:
        conn.close()
        return []

    results = []

    for d in does:
        # doe stats
        cur.execute("""
            SELECT COUNT(*) AS c, COALESCE(SUM(litter_size),0) AS s
            FROM breedings
            WHERE doe_id=? AND kindling_date IS NOT NULL
        """, (d["id"],))
        br = cur.fetchone()
        litters = br["c"]
        total_kits = int(br["s"] or 0)
        avg_litter = (total_kits / litters) if litters > 0 else 0
        has_g, daily_g, _, _ = get_growth_stats(d["name"])

        for b in bucks:
            severity, _ = assess_inbreeding(d["name"], b["name"])
            if severity == "danger":
                continue  # skip

            score = 0.0
            # inbreeding safety
            if severity == "none":
                score += 5.0
            elif severity == "warning":
                score += 1.0

            # doe productivity
            score += avg_litter * 2.0
            score += litters * 1.0

            if has_g and daily_g:
                score += daily_g / 10.0  # small boost for better growth

            # buck: number of children in DB
            cur.execute("SELECT COUNT(*) AS c FROM rabbits WHERE father_id=?", (b["id"],))
            off = cur.fetchone()["c"]
            score += off * 0.3

            results.append((score, d["name"], b["name"], severity))

    conn.close()

    results.sort(key=lambda x: x[0], reverse=True)
    return results[:limit]


def compute_achievements():
    """Calculate unlocked achievements based on farm data."""
    conn = get_db()
    cur = conn.cursor()

    # Litters & kits
    cur.execute("SELECT COUNT(*) AS c FROM breedings WHERE kindling_date IS NOT NULL")
    litters = cur.fetchone()["c"]

    cur.execute("SELECT COALESCE(SUM(litter_size),0) AS s FROM breedings WHERE litter_size IS NOT NULL")
    total_kits = int(cur.fetchone()["s"] or 0)

    # Rabbits & sales
    cur.execute("SELECT COUNT(*) AS c FROM rabbits")
    rabbits = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM sales")
    sales = cur.fetchone()["c"]

    income, expenses, profit = get_profit_summary(None)
    feed_kg, feed_cost = get_feed_stats(None)

    conn.close()

    achievements = []

    # Breeding
    if litters >= 1:
        achievements.append("🏅 Starter Breeder: recorded your first litter.")
    if litters >= 10:
        achievements.append("🏆 Pro Breeder: 10 litters recorded.")
    if total_kits >= 50:
        achievements.append("🐇 Baby Boom: 50 kits recorded.")
    if total_kits >= 200:
        achievements.append("🐰 Mega Farm: 200+ kits recorded.")

    # Sales & money
    if sales >= 1:
        achievements.append("💸 First Sale: sold your first rabbit.")
    if profit > 0:
        achievements.append("💰 In the Green: overall profit is positive.")
    if profit > 500:
        achievements.append("💎 Cash Flow: profit over 500.")

    # Feed & management
    if feed_kg >= 100:
        achievements.append("🌾 Feed Master: logged 100+ kg of feed.")
    if rabbits >= 20:
        achievements.append("📦 Busy Farm: 20+ rabbits in database.")

    if not achievements:
        achievements.append("No achievements yet – start logging litters, weights, and sales to unlock badges!")

    return achievements


def get_climate_warning_message():
    """Return a message about heat/cold risk based on last set temperature."""
    val = get_setting("last_temp_c")
    if val is None:
        return (
            "No temperature data yet.\n"
            "Use /settemp C to log current temperature (example: /settemp 32)."
        )
    try:
        t = float(val)
    except ValueError:
        return "Stored temperature is invalid. Set again with /settemp C."

    lines = [f"Last recorded temperature: {t:.1f}°C"]

    if t >= 32:
        lines.append("🔥 High heat stress risk! Make sure there is shade, ventilation, and plenty of water.")
    elif 28 <= t < 32:
        lines.append("🌡 Warm conditions. Watch for heat stress; avoid heavy handling or transport.")
    elif 10 <= t < 28:
        lines.append("✅ Comfortable zone for most rabbits.")
    elif 0 <= t < 10:
        lines.append("❄️ Cool weather. Ensure dry bedding and protection from drafts.")
    else:  # t < 0
        lines.append("🥶 Cold stress risk! Add extra bedding, block drafts, and check water isn't frozen.")

    return "\n".join(lines)


def get_climate_warning_short():
    """Short one-line warning for daily summary."""
    val = get_setting("last_temp_c")
    if val is None:
        return None
    try:
        t = float(val)
    except ValueError:
        return None

    if t >= 32:
        return f"{t:.1f}°C – 🔥 High heat stress risk."
    if 28 <= t < 32:
        return f"{t:.1f}°C – 🌡 Warm, watch for heat stress."
    if 10 <= t < 28:
        return f"{t:.1f}°C – ✅ Comfortable zone."
    if 0 <= t < 10:
        return f"{t:.1f}°C – ❄️ Cool, protect from drafts."
    return f"{t:.1f}°C – 🥶 Cold stress risk – add bedding."


# ================== TELEGRAM HANDLERS ==================

# ================== ADD-RABBIT WIZARD ==================

async def addrabbit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start interactive rabbit creation."""
    # make sure only you can use it
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    context.user_data["new_rabbit"] = {}
    await update.message.reply_text(
        "🐰 Let's add a new rabbit.\n\n"
        "First, send the *name* of the rabbit:",
        parse_mode="Markdown",
    )
    return ADD_NAME


async def addrabbit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please send a non-empty name 🙂")
        return ADD_NAME

    context.user_data.setdefault("new_rabbit", {})["name"] = name
    await update.message.reply_text(
        f"Got it: *{name}*.\n\nIs it male or female? Type *M* or *F*.",
        parse_mode="Markdown",
    )
    return ADD_SEX


async def addrabbit_sex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    sex_raw = update.message.text.strip().upper()
    if sex_raw not in ("M", "F"):
        await update.message.reply_text("Sex must be *M* or *F*. Please type again.", parse_mode="Markdown")
        return ADD_SEX

    data = context.user_data.get("new_rabbit", {})
    name = data.get("name")

    # Try to create rabbit now
    ok = add_rabbit(name, sex_raw)
    if not ok:
        await update.message.reply_text(
            f"❌ A rabbit with the name *{name}* already exists. Wizard cancelled.",
            parse_mode="Markdown",
        )
        context.user_data.pop("new_rabbit", None)
        return ConversationHandler.END

    data["sex"] = sex_raw
    context.user_data["new_rabbit"] = data

    await update.message.reply_text(
        "✅ Rabbit created in database!\n\n"
        "Now send the *cage number* (for example: A1).\n"
        "If you want to skip cage, type `-`.",
        parse_mode="Markdown",
    )
    return ADD_CAGE


async def addrabbit_cage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    cage_raw = update.message.text.strip()
    data = context.user_data.get("new_rabbit", {})
    name = data.get("name")

    cage = None if cage_raw in ("-", "skip", "SKIP") else cage_raw
    data["cage"] = cage
    context.user_data["new_rabbit"] = data

    if cage:
        await update.message.reply_text(
            f"✅ Cage set to *{cage}*.\n\n"
            "Now send *section* (for example: left / right / top).\n"
            "If you want to skip section, type `-`.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "Cage skipped.\n\n"
            "Now send *section* (for example: left / right / top).\n"
            "If you want to skip section, type `-`.",
            parse_mode="Markdown",
        )
    return ADD_SECTION


async def addrabbit_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    section_raw = update.message.text.strip()
    data = context.user_data.get("new_rabbit", {})
    name = data.get("name")
    cage = data.get("cage")

    section = None if section_raw in ("-", "skip", "SKIP") else section_raw
    data["section"] = section
    context.user_data["new_rabbit"] = data

    # If we have cage/section, store them to DB now
    if cage or section:
        set_cage_section(name, cage or "", section)

    if section:
        await update.message.reply_text(
            f"✅ Section set to *{section}*.\n\n"
            "Finally, send the *weight in kg* (example: 2.3).\n"
            "If you want to skip weight, type `-`.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "Section skipped.\n\n"
            "Finally, send the *weight in kg* (example: 2.3).\n"
            "If you want to skip weight, type `-`.",
            parse_mode="Markdown",
        )
    return ADD_WEIGHT


async def addrabbit_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    data = context.user_data.get("new_rabbit", {})
    name = data.get("name")
    sex = data.get("sex")
    cage = data.get("cage")
    section = data.get("section")

    text = update.message.text.strip()
    weight = None

    if text not in ("-", "skip", "SKIP"):
        try:
            weight = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "Weight must be a number (example: 2.3). Try again or type `-` to skip.",
                parse_mode="Markdown",
            )
            return ADD_WEIGHT

    if weight is not None:
        add_weight(name, weight)

    # Build summary
    lines = [f"🎉 Rabbit *{name}* added!"]
    lines.append(f"- Sex: {sex}")
    if cage:
        lines.append(f"- Cage: {cage}")
    if section:
        lines.append(f"- Section: {section}")
    if weight is not None:
        lines.append(f"- Weight: {weight} kg")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # clean wizard data
    context.user_data.pop("new_rabbit", None)
    return ConversationHandler.END


async def addrabbit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow user to cancel wizard with /cancel."""
    context.user_data.pop("new_rabbit", None)
    await update.message.reply_text("❌ Add-rabbit wizard cancelled.")
    return ConversationHandler.END
async def start_add_rabbit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🐰 Let's add a new rabbit!\n\nWhat is the name?")
    return ADD_NAME


async def add_rabbit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("Sex? Send M or F")
    return ADD_SEX


async def add_rabbit_sex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sex = update.message.text.strip().upper()
    if sex not in ("M", "F"):
        await update.message.reply_text("❌ Please send M or F")
        return ADD_SEX

    context.user_data["sex"] = sex
    await update.message.reply_text("Enter weight in kg (example: 2.4)")
    return ADD_WEIGHT


async def add_rabbit_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Please enter a number (example: 2.5)")
        return ADD_WEIGHT

    context.user_data["weight"] = weight
    await update.message.reply_text("Enter cage number")
    return ADD_CAGE


async def add_rabbit_cage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cage = update.message.text.strip()

    name = context.user_data["name"]
    sex = context.user_data["sex"]
    weight = context.user_data["weight"]

    add_rabbit(name, sex)
    add_weight(name, weight)
    set_cage_section(name, cage)

    await update.message.reply_text(
        f"✅ Rabbit added!\n\n"
        f"Name: {name}\n"
        f"Sex: {sex}\n"
        f"Weight: {weight} kg\n"
        f"Cage: {cage}"
    )

    context.user_data.clear()
    return ConversationHandler.END


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    # 1) Send the big help text
    await update.message.reply_text(
        "🐰 Rabbit Farm Bot\n\n"
        "Rabbits:\n"
        "/addrabbit – interactive wizard\n"
"/addrabbit_fast NAME M/F – quick add\n"
"/rabbits\n"
        "/cancel – cancel current wizard\n"


        "/active\n"
        "/setcage NAME CAGE [SECTION]\n"
        "/setparents CHILD MOTHER FATHER\n"
        "/checkpair R1 R2\n"
        "/markdead NAME [REASON]\n"
        "\nBreeding & litters:\n"
        "/breed DOE BUCK\n"
        "/forcebreed DOE BUCK  (ignore inbreeding warning)\n"
        "/suggestbreed\n"
        "/kindling DOE LITTER_SIZE [LITTERNAME]\n"
        "/litters DOE\n"
        "/littername DOE LITTERNAME\n"
        "/nextdue DOE\n"
        "/today\n"
        "/weaning\n"
        "\nHealth & weights:\n"
        "/health NAME note...\n"
        "/healthlog NAME\n"
        "/weight NAME KG\n"
        "/weightlog NAME\n"
        "/growth NAME\n"
        "/growthchart NAME\n"
        "\nMoney & feed:\n"
        "/sell NAME PRICE [BUYER]\n"
        "/expense AMOUNT CATEGORY [NOTE]\n"
        "/electric AMOUNT [NOTE]\n"
        "/feed KG COST [NOTE]\n"
        "/profit\n"
        "/profitmonth YYYY-MM\n"
        "/profityear YYYY\n"
        "/feedstats\n"
        "/feedmonth YYYY-MM\n"
        "\nTasks:\n"
        "/remind YYYY-MM-DD TEXT\n"
        "/tasklist\n"
        "/donetask ID\n"
        "\nInfo & analytics:\n"
        "/info NAME\n"
        "/stats\n"
        "/farmsummary\n"
        "/tree NAME\n"
        "/lineperformance NAME\n"
        "/keep NAME\n"
        "\nClimate:\n"
        "/settemp C   (example: /settemp 32)\n"
        "/climatealert\n"
        "\nPhotos:\n"
        "Send a photo with caption = NAME to assign it\n"
        "/photo NAME (show stored photo)\n"
        "\nData & backup:\n"
        "/export_rabbits\n"
        "/export_breedings\n"
        "/export_sales\n"
        "/export_expenses\n"
        "/backupdb\n"
        "\nGamified:\n"
        "/achievements\n"
        "\nAutomation:\n"
        "/subscribe\n"
        "/unsubscribe\n"
        "\nDebug:\n"
        "/whoami  (shows your Telegram user ID)"
    )

    # 2) Show the button menu right after the help text
    await menu_cmd(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)

async def achievements_cmd_internal(target, context: ContextTypes.DEFAULT_TYPE):
    """
    Internal helper: works for both CallbackQuery (menu button)
    and normal Message (/achievements command).
    """
    achievements = get_achievements()

    if not achievements:
        text = "🏆 No achievements unlocked yet.\nKeep working on your farm!"
    else:
        count = len(achievements)
        level = max(1, (count + 1) // 2)

        lines = [
            f"🏆 *Your achievements* (Level {level})",
            "",
        ]
        for row in achievements:
            desc = describe_achievement(row["key"])
            lines.append(f"- {desc} (since {row['unlocked_date']})")
        text = "\n".join(lines)

    # target can be a Message (update) or a CallbackQuery
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode="Markdown")
    else:
        await target.message.reply_text(text, parse_mode="Markdown")


async def achievements_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # If you use owner_guard, keep this; otherwise you can delete this 'if' block
    if "owner_guard" in globals():
        if not await owner_guard(update, context):
            return

    await achievements_cmd_internal(update, context)
MAIN_MENU_TEXT = (
    "🐰 *Rabbit Farm OS*\n\n"
    "Choose a section:"
)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "owner_guard" in globals():
        if not await owner_guard(update, context):
            return

    keyboard = [
        [
            InlineKeyboardButton("🐰 Rabbits", callback_data="menu_rabbits"),
            InlineKeyboardButton("🧬 Breeding", callback_data="menu_breeding"),
        ],
        [
            InlineKeyboardButton("💰 Finance", callback_data="menu_finance"),
            InlineKeyboardButton("🌡 Climate", callback_data="menu_climate"),
        ],
        [
            InlineKeyboardButton("📊 Stats", callback_data="menu_stats"),
            InlineKeyboardButton("🏆 Achievements", callback_data="menu_achievements"),
        ],
    ]
    await update.message.reply_text(
        MAIN_MENU_TEXT,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_rabbits":
        await query.edit_message_text(
            "🐰 *Rabbits menu*\n\n"
            "/rabbits – list all\n"
            "/active – active only\n"
            "/info NAME – rabbit info\n"
            "/growth NAME – growth analysis\n"
            "/growthchart NAME – growth chart\n"
            "/photo NAME – show photo\n",
            parse_mode="Markdown",
        )

    elif data == "menu_breeding":
        await query.edit_message_text(
            "🧬 *Breeding menu*\n\n"
            "/checkpair R1 R2 – check relation\n"
            "/breed DOE BUCK – safe breeding\n"
            "/forcebreed DOE BUCK – force breeding\n"
            "/kindling DOE SIZE [NAME] – litter\n"
            "/litters DOE – litter history\n"
            "/nextdue DOE – next due\n"
            "/today – due today\n"
            "/weaning – weaning today\n"
            "/lineperformance NAME – line stats\n",
            parse_mode="Markdown",
        )

    elif data == "menu_finance":
        await query.edit_message_text(
            "💰 *Finance & feed menu*\n\n"
            "/sell NAME PRICE [BUYER]\n"
            "/expense AMOUNT CATEGORY [NOTE]\n"
            "/electric AMOUNT [NOTE]\n"
            "/feed KG COST [NOTE]\n"
            "/profit – all time\n"
            "/profitmonth YYYY-MM\n"
            "/profityear YYYY\n"
            "/feedstats – all time\n"
            "/feedmonth YYYY-MM\n",
            parse_mode="Markdown",
        )

    elif data == "menu_climate":
        await query.edit_message_text(
            "🌡 *Climate & environment*\n\n"
            "/settemp C – set temperature\n"
            "/sethumidity PERCENT – set humidity (if you added it)\n"
            "/climatealert – risk check\n",
            parse_mode="Markdown",
        )

    elif data == "menu_stats":
        await query.edit_message_text(
            "📊 *Stats & summaries*\n\n"
            "/stats – rabbit stats\n"
            "/farmsummary – farm + finance + feed\n"
            "/achievements – your badges\n",
            parse_mode="Markdown",
        )

    elif data == "menu_achievements":
        await achievements_cmd_internal(query, context)



async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """This one is NOT owner-locked so you can always see your ID."""
    uid = update.effective_user.id
    await update.message.reply_text(f"Your user ID is: {uid}\n\n"
                                    "Put this number into OWNER_ID in rabbit_bot.py to lock the bot to you.")

# ================== ADD-RABBIT WIZARD ==================

async def addrabbit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: ask for name."""
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    await update.message.reply_text(
        "➕ Adding a new rabbit.\n\n"
        "1️⃣ Send the *name* of the rabbit:",
        parse_mode="Markdown",
    )
    return ADD_NAME


async def addrabbit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Got name, now ask for sex."""
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please send a non-empty name.")
        return ADD_NAME

    context.user_data["name"] = name
    await update.message.reply_text(
        f"Name set to *{name}*.\n\n"
        "2️⃣ Is it male or female? Reply with *M* or *F*:",
        parse_mode="Markdown",
    )
    return ADD_SEX


async def addrabbit_sex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Got sex, now ask for cage."""
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    sex = update.message.text.strip().upper()
    if sex not in ("M", "F"):
        await update.message.reply_text("Please reply with M or F.")
        return ADD_SEX

    context.user_data["sex"] = sex
    await update.message.reply_text(
        "3️⃣ Which *cage number* is this rabbit in?\n\n"
        "Example: `A1`, `3`, `C-02`",
        parse_mode="Markdown",
    )
    return ADD_CAGE


async def addrabbit_cage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Got cage, now ask for section (optional)."""
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    cage = update.message.text.strip()
    if not cage:
        await update.message.reply_text("Please send a cage number.")
        return ADD_CAGE

    context.user_data["cage"] = cage
    await update.message.reply_text(
        "4️⃣ Section (optional).\n"
        "If you use sections (e.g. *left*, *right*, *top*), send it now.\n"
        "If you don't want to set a section, type *skip*.",
        parse_mode="Markdown",
    )
    return ADD_SECTION


async def addrabbit_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Got section (or skip), now ask for weight (optional)."""
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.lower() == "skip":
        context.user_data["section"] = None
    else:
        context.user_data["section"] = text

    await update.message.reply_text(
        "5️⃣ Weight in *kg* (optional).\n"
        "Example: `2.3`\n"
        "If you don't want to set weight now, type *skip*.",
        parse_mode="Markdown",
    )
    return ADD_WEIGHT


async def addrabbit_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final step: create rabbit, then show summary."""
    if not await ensure_owner(update, context):
        return ConversationHandler.END

    text = update.message.text.strip()
    weight = None
    if text.lower() != "skip":
        try:
            weight = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "Please send a number (like 2.3) for weight in kg, "
                "or type *skip*.",
                parse_mode="Markdown",
            )
            return ADD_WEIGHT

    name = context.user_data.get("name")
    sex = context.user_data.get("sex")
    cage = context.user_data.get("cage")
    section = context.user_data.get("section")

    if not name or not sex:
        await update.message.reply_text("Something went wrong, cancelling.")
        return ConversationHandler.END

    # 1) Create rabbit
    ok = add_rabbit(name, sex)
    if not ok:
        await update.message.reply_text(
            "❌ A rabbit with that name already exists. Cancelling."
        )
        return ConversationHandler.END

    # 2) Set cage/section
    if cage:
        set_cage_section(name, cage, section)

    # 3) Set weight if provided
    if weight is not None:
        add_weight(name, weight)

    # 4) Build nice summary message
    details = []
    if cage:
        loc = f"cage {cage}"
        if section:
            loc += f" / section {section}"
        details.append(loc)
    if weight is not None:
        details.append(f"weight {weight} kg")

    msg = f"✅ Rabbit *{name}* ({sex}) added."
    if details:
        msg += "\n" + ", ".join(details)

    await update.message.reply_text(msg, parse_mode="Markdown")
    return ConversationHandler.END


async def addrabbit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow user to cancel the wizard with /cancel."""
    if update.message:
        await update.message.reply_text("❌ Add-rabbit cancelled.")
    return ConversationHandler.END



# ---- Rabbits ----


async def rabbits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    rows = list_rabbits(active_only=False)
    if not rows:
        await update.message.reply_text("No rabbits in database.")
        return

    lines = []
    for r in rows:
        cage = r["cage"] if r["cage"] else "—"
        section = r["section"] if r["section"] else "—"
        status = r["status"] if r["status"] else "unknown"

        lines.append(
            f"🐰 *{r['name']}*\n"
            f"Sex: {r['sex']}\n"
            f"Cage: {cage}\n"
            f"Section: {section}\n"
            f"Status: {status}\n"
            f"--------------------------"
        )

    await update.message.reply_text(
        "🐰 *All Rabbits (Full View)*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    rows = list_rabbits(active_only=True)
    if not rows:
        await update.message.reply_text("No active rabbits.")
        return
    lines = [f"{r['name']} ({r['sex']})" for r in rows]
    await update.message.reply_text("🐰 Active rabbits:\n" + "\n".join(lines))


async def setcage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 3:
        await update.message.reply_text("Usage: /setcage NAME CAGE [SECTION]")
        return
    name = parts[1]
    cage = parts[2]
    section = parts[3] if len(parts) > 3 else None
    msg = set_cage_section(name, cage, section)
    await update.message.reply_text(msg)


async def setparents_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 4:
        await update.message.reply_text("Usage: /setparents CHILD MOTHER FATHER")
        return
    child, mother, father = parts[1], parts[2], parts[3]
    msg = update_rabbit_parents(child, mother, father)
    await update.message.reply_text(msg)


async def checkpair_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 3:
        await update.message.reply_text("Usage: /checkpair RABBIT1 RABBIT2")
        return
    msg = checkpair_inbreeding(parts[1], parts[2])
    await update.message.reply_text(msg)


async def markdead_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /markdead NAME [REASON]")
        return
    name = parts[1]
    reason = parts[2] if len(parts) > 2 else None
    msg = mark_dead(name, reason)
    await update.message.reply_text(msg)


# ---- Breeding & litters ----

async def breed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 3:
        await update.message.reply_text("Usage: /breed DOE BUCK")
        return
    doe, buck = parts[1], parts[2]

    severity, warning = assess_inbreeding(doe, buck)
    if severity == "error":
        await update.message.reply_text(warning)
        return
    if severity == "danger":
        await update.message.reply_text(
            warning
            + "\n\n❗ Dangerous inbreeding. Breeding blocked.\n"
              "If you still really want this, use:\n"
              f"/forcebreed {doe} {buck}"
        )
        return
    elif severity == "warning":
        await update.message.reply_text(warning)

    msg = add_breeding(doe, buck)
    await update.message.reply_text(msg)


async def forcebreed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Same as /breed but ignores inbreeding warnings (still blocks errors)."""
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 3:
        await update.message.reply_text("Usage: /forcebreed DOE BUCK")
        return
    doe, buck = parts[1], parts[2]

    severity, warning = assess_inbreeding(doe, buck)
    if severity == "error":
        await update.message.reply_text(warning)
        return

    msg = add_breeding(doe, buck)
    if severity in ("danger", "warning"):
        await update.message.reply_text("⚠️ Forced breeding despite relation:\n" + warning + "\n\n" + msg)
    else:
        await update.message.reply_text("⚠️ Forced breeding (no close relation detected):\n" + msg)


async def kindling_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split(maxsplit=3)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /kindling DOE LITTER_SIZE [LITTERNAME]")
        return
    doe = parts[1]
    try:
        size = int(parts[2])
    except ValueError:
        await update.message.reply_text("LITTER_SIZE must be a number.")
        return
    litter_name = parts[3] if len(parts) > 3 else None
    msg = record_kindling(doe, size, litter_name)
    await update.message.reply_text(msg)


async def litters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /litters DOE")
        return
    doe_name = parts[1]
    doe, rows = get_litters_for_doe(doe_name)
    if not doe:
        await update.message.reply_text("❌ Doe not found.")
        return
    if not rows:
        await update.message.reply_text("No litters recorded for this doe.")
        return
    lines = []
    for r in rows:
        ln = r["litter_name"] or "(no name)"
        lines.append(
            f"{r['kindling_date']}: {ln} – {r['litter_size']} kits (buck: {r['buck_name']})"
        )
    await update.message.reply_text(f"🍼 Litters for {doe_name}:\n" + "\n".join(lines))


async def littername_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /littername DOE LITTERNAME")
        return
    doe, ln = parts[1], parts[2]
    msg = set_litter_name_for_latest(doe, ln)
    await update.message.reply_text(msg)


async def nextdue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /nextdue DOE")
        return
    doe = parts[1]
    nxt = get_next_due_for_doe(doe)
    if not nxt:
        await update.message.reply_text("No upcoming due date for this doe.")
        return
    await update.message.reply_text(
        f"Next due for {doe}: {nxt['expected_due_date']} (bred on {nxt['mating_date']})"
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    dues = get_due_today()
    tasks = get_tasks_for_date(date.today())

    lines = [f"🐰 Today: {date.today().isoformat()}"]

    if dues:
        lines.append("\n🍼 Kindlings due today:")
        lines.extend([f"- {r['name']}" for r in dues])
    else:
        lines.append("\nNo kindlings due today.")

    weans = get_weaning_today()
    if weans:
        lines.append("\n🐇 Weaning today:")
        lines.extend([f"- {r['name']}" for r in weans])
    else:
        lines.append("\nNo weaning scheduled today.")

    if tasks:
        lines.append("\n📌 Tasks for today:")
        for t in tasks:
            line = f"- #{t['id']} [{t['task_date']}] {t['title']}"
            if t["note"]:
                line += f" – {t['note']}"
            lines.append(line)
    else:
        lines.append("\nNo tasks for today.")

    climate_short = get_climate_warning_short()
    if climate_short:
        lines.append("\n🌡 Climate alert:")
        lines.append(climate_short)

    await update.message.reply_text("\n".join(lines))


async def weaning_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    rows = get_weaning_today()
    if not rows:
        await update.message.reply_text("No weaning scheduled for today.")
        return
    lines = [f"- {r['name']}" for r in rows]
    await update.message.reply_text("🐇 Weaning today for:\n" + "\n".join(lines))


async def suggestbreed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    pairs = suggest_breeding_pairs(limit=5)
    if not pairs:
        await update.message.reply_text(
            "No suggested pairs.\nMake sure you have active does and bucks, and some breeding data."
        )
        return

    lines = ["🧠 Suggested breeding pairs (best first):"]
    for score, doe, buck, severity in pairs:
        rel = {
            "none": "no close relation",
            "warning": "cousin-level relation",
            "danger": "danger (should be blocked)",
        }.get(severity, severity)
        lines.append(f"- {doe} × {buck}  | score {score:.1f} | {rel}")
    lines.append("\nScore considers: inbreeding safety, doe litter history, growth, and buck offspring count.")
    await update.message.reply_text("\n".join(lines))


# ---- Health & weights ----

async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /health NAME note...")
        return
    name = parts[1]
    note = parts[2]
    msg = add_health_record(name, note)
    await update.message.reply_text(msg)


async def healthlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /healthlog NAME")
        return
    rabbit, rows = get_health_log(parts[1], limit=10)
    if not rabbit:
        await update.message.reply_text("❌ Rabbit not found.")
        return
    if not rows:
        await update.message.reply_text("No health records.")
        return
    lines = [f"{r['record_date']}: {r['note']}" for r in rows]
    await update.message.reply_text(f"🩺 Health log for {rabbit['name']}:\n" + "\n".join(lines))


async def weight_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 3:
        await update.message.reply_text("Usage: /weight NAME KG")
        return
    name = parts[1]
    try:
        w = float(parts[2])
    except ValueError:
        await update.message.reply_text("KG must be a number.")
        return
    msg = add_weight(name, w)
    await update.message.reply_text(msg)


async def weightlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /weightlog NAME")
        return
    rabbit, rows = get_weight_log(parts[1], limit=10)
    if not rabbit:
        await update.message.reply_text("❌ Rabbit not found.")
        return
    if not rows:
        await update.message.reply_text("No weight records.")
        return
    lines = [f"{r['weigh_date']}: {r['weight_kg']} kg" for r in rows]
    await update.message.reply_text(f"⚖️ Weight log for {rabbit['name']}:\n" + "\n".join(lines))


async def growth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /growth NAME")
        return
    name = parts[1]
    msg = compute_growth_message(name)
    await update.message.reply_text(msg)


async def growthchart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /growthchart NAME")
        return
    name = parts[1]
    msg = build_growth_chart_ascii(name)
    await update.message.reply_text(msg)


# ---- Money & feed ----

async def sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split(maxsplit=3)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /sell NAME PRICE [BUYER]")
        return
    name = parts[1]
    try:
        price = float(parts[2])
    except ValueError:
        await update.message.reply_text("PRICE must be a number.")
        return
    buyer = parts[3] if len(parts) > 3 else None
    msg = record_sale(name, price, buyer)
    await update.message.reply_text(msg)


async def expense_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split(maxsplit=3)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /expense AMOUNT CATEGORY [NOTE]")
        return
    try:
        amount = float(parts[1])
    except ValueError:
        await update.message.reply_text("AMOUNT must be a number.")
        return
    category = parts[2]
    note = parts[3] if len(parts) > 3 else None
    msg = add_expense(amount, category, note)
    await update.message.reply_text(msg)


async def electric_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /electric AMOUNT [NOTE]")
        return
    try:
        amount = float(parts[1])
    except ValueError:
        await update.message.reply_text("AMOUNT must be a number.")
        return
    note = parts[2] if len(parts) > 2 else None
    msg = add_expense(amount, "electricity", note)
    await update.message.reply_text(msg)


async def feed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split(maxsplit=3)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /feed KG COST [NOTE]")
        return
    try:
        kg = float(parts[1])
        cost = float(parts[2])
    except ValueError:
        await update.message.reply_text("KG and COST must be numbers.")
        return
    note = parts[3] if len(parts) > 3 else None
    msg = add_feed(kg, cost, note)
    await update.message.reply_text(msg)


async def profit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    inc, exp, prof = get_profit_summary(None)
    await update.message.reply_text(
        f"💰 Profit (all time):\nIncome: {inc}\nExpenses: {exp}\nProfit: {prof}"
    )


async def profitmonth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /profitmonth YYYY-MM")
        return
    period = parts[1]
    inc, exp, prof = get_profit_summary(period)
    await update.message.reply_text(
        f"💰 Profit for {period}:\nIncome: {inc}\nExpenses: {exp}\nProfit: {prof}"
    )


async def profityear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /profityear YYYY")
        return
    period = parts[1]
    inc, exp, prof = get_profit_summary(period)
    await update.message.reply_text(
        f"💰 Profit for {period}:\nIncome: {inc}\nExpenses: {exp}\nProfit: {prof}"
    )


async def feedstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    kg, cost = get_feed_stats(None)
    await update.message.reply_text(
        f"🌾 Feed stats (all time):\nTotal feed: {kg} kg\nCost: {cost}"
    )


async def feedmonth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /feedmonth YYYY-MM")
        return
    period = parts[1]
    kg, cost = get_feed_stats(period)
    await update.message.reply_text(
        f"🌾 Feed stats for {period}:\nTotal feed: {kg} kg\nCost: {cost}"
    )


# ---- Exports & backup ----

async def export_rabbits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    headers = ["id", "name", "sex", "mother_id", "father_id",
               "cage", "section", "status", "death_date", "death_reason", "photo_file_id"]
    path = export_table_to_csv("SELECT * FROM rabbits ORDER BY id", None, headers, "rabbits")
    if not path:
        await update.message.reply_text("No rabbits to export.")
        return
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=open(path, "rb"),
        filename="rabbits_export.csv",
        caption="🐰 Rabbits export"
    )
    os.remove(path)


async def export_breedings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    headers = ["id", "doe_id", "buck_id", "mating_date",
               "expected_due_date", "kindling_date", "litter_size", "weaning_date", "litter_name"]
    path = export_table_to_csv("SELECT * FROM breedings ORDER BY id", None, headers, "breedings")
    if not path:
        await update.message.reply_text("No breedings to export.")
        return
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=open(path, "rb"),
        filename="breedings_export.csv",
        caption="🍼 Breedings export"
    )
    os.remove(path)


async def export_sales_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    headers = ["id", "rabbit_id", "sale_date", "price", "buyer"]
    path = export_table_to_csv("SELECT * FROM sales ORDER BY id", None, headers, "sales")
    if not path:
        await update.message.reply_text("No sales to export.")
        return
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=open(path, "rb"),
        filename="sales_export.csv",
        caption="💸 Sales export"
    )
    os.remove(path)


async def export_expenses_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    headers = ["id", "exp_date", "category", "amount", "note"]
    path = export_table_to_csv("SELECT * FROM expenses ORDER BY id", None, headers, "expenses")
    if not path:
        await update.message.reply_text("No expenses to export.")
        return
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=open(path, "rb"),
        filename="expenses_export.csv",
        caption="💰 Expenses export"
    )
    os.remove(path)


async def backupdb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    path = get_backup_db_path()
    if not path:
        await update.message.reply_text("Database file not found.")
        return
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=open(path, "rb"),
        filename="rabbits.db",
        caption="📦 Database backup"
    )


# ---- Tasks ----

async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /remind YYYY-MM-DD TEXT")
        return
    d_str = parts[1]
    text = parts[2]
    try:
        _ = date.fromisoformat(d_str)
    except ValueError:
        await update.message.reply_text("Date must be in YYYY-MM-DD format.")
        return
    msg = add_task(d_str, text, None)
    await update.message.reply_text(msg)


async def tasklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    rows = get_upcoming_tasks(limit=20)
    if not rows:
        await update.message.reply_text("No upcoming tasks.")
        return
    lines = []
    for t in rows:
        line = f"#{t['id']} [{t['task_date']}] {t['title']}"
        if t["note"]:
            line += f" – {t['note']}"
        lines.append(line)
    await update.message.reply_text("📌 Upcoming tasks:\n" + "\n".join(lines))


async def donetask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /donetask ID")
        return
    try:
        tid = int(parts[1])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return
    ok = mark_task_done(tid)
    if ok:
        await update.message.reply_text("✅ Task marked as done.")
    else:
        await update.message.reply_text("❌ Task not found.")


# ---- Info & analytics ----

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /info NAME")
        return
    msg = get_info_message(parts[1])
    await update.message.reply_text(msg)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    msg = get_stats_message()
    await update.message.reply_text(msg)


async def farmsummary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    msg = get_farmsummary_message()
    await update.message.reply_text(msg)


async def tree_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /tree NAME")
        return
    name = parts[1]
    msg = build_family_tree(name)
    await update.message.reply_text(msg)


async def lineperformance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /lineperformance NAME")
        return
    name = parts[1]
    msg = get_line_performance_message(name)
    await update.message.reply_text(msg)


async def keep_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /keep NAME")
        return
    name = parts[1]
    msg = decide_keep_or_sell(name)
    await update.message.reply_text(msg)


# ---- Climate ----

async def settemp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /settemp C\nExample: /settemp 32")
        return
    try:
        t = float(parts[1])
    except ValueError:
        await update.message.reply_text("Temperature must be a number, in °C.")
        return
    set_setting("last_temp_c", str(t))
    await update.message.reply_text(
        f"✅ Temperature set to {t:.1f}°C.\nUse /climatealert to see heat/cold risk."
    )


async def climatealert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    msg = get_climate_warning_message()
    await update.message.reply_text(msg)


# ---- Photos ----

async def photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    """Send stored photo of a rabbit."""
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /photo NAME")
        return
    name = parts[1]
    r = get_rabbit(name)
    if not r:
        await update.message.reply_text("❌ Rabbit not found.")
        return
    if not r["photo_file_id"]:
        await update.message.reply_text(
            "No photo stored for this rabbit.\n"
            "Send a photo with caption = NAME to assign one."
        )
        return
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=r["photo_file_id"],
        caption=f"🐰 {name}"
    )


async def photo_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    """Handle incoming photos: caption must start with rabbit name."""
    if not update.message or not update.message.photo:
        return

    caption = (update.message.caption or "").strip()
    if not caption:
        await update.message.reply_text(
            "Please write the rabbit's NAME in the photo caption to assign it.\n"
            "Example: send photo with caption: Luna"
        )
        return

    name = caption.split()[0]
    photo = update.message.photo[-1]  # highest resolution
    file_id = photo.file_id

    ok, msg = set_rabbit_photo(name, file_id)
    await update.message.reply_text(msg)


# ---- Gamified achievements ----

async def achievements_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    achievements = compute_achievements()
    await update.message.reply_text("🏅 Achievements:\n" + "\n".join(achievements))


# ---- Subscribe / Unsubscribe (daily summary) ----

async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    dues = get_due_today()
    weans = get_weaning_today()
    tasks = get_tasks_for_date(date.today())

    lines = [f"🐰 Daily farm summary for {date.today().isoformat()}"]

    if dues:
        lines.append("\n🍼 Kindlings due today:")
        for d in dues:
            lines.append(f"- {d['name']}")
    else:
        lines.append("\nNo kindlings due today.")

    if weans:
        lines.append("\n🐇 Weaning today:")
        for w in weans:
            lines.append(f"- {w['name']}")
    else:
        lines.append("\nNo weaning scheduled today.")

    if tasks:
        lines.append("\n📌 Tasks for today:")
        for t in tasks:
            line = f"- #{t['id']} {t['title']}"
            if t["note"]:
                line += f" – {t['note']}"
            lines.append(line)
    else:
        lines.append("\nNo tasks for today.")

    climate_short = get_climate_warning_short()
    if climate_short:
        lines.append("\n🌡 Climate alert:")
        lines.append(climate_short)

    try:
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
    except Exception as e:
        logging.error("Error in daily_job: %s", e)


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    if context.job_queue is None:
        await update.message.reply_text(
            "Job system is not available on this server, can't subscribe."
        )
        return

    chat_id = update.effective_chat.id
    job_name = f"daily_{chat_id}"

    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    run_time = time(hour=9, minute=0, second=0)
    context.job_queue.run_daily(
        daily_job,
        time=run_time,
        name=job_name,
        chat_id=chat_id,
    )

    await update.message.reply_text(
        "✅ Subscribed to daily farm summary at 09:00.\nUse /unsubscribe to stop."
    )


async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_owner(update, context):
        return

    if context.job_queue is None:
        await update.message.reply_text(
            "Job system is not available on this server, can't unsubscribe."
        )
        return

    chat_id = update.effective_chat.id
    job_name = f"daily_{chat_id}"
    jobs = context.job_queue.get_jobs_by_name(job_name)

    if not jobs:
        await update.message.reply_text("You are not subscribed.")
        return

    for job in jobs:
        job.schedule_removal()

    await update.message.reply_text("❌ Unsubscribed from daily summary.")


# ================== HEALTHCHECK HTTP SERVER FOR RENDER ==================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        # So Render's HEAD check doesn't show 501
        self.send_response(200)
        self.end_headers()


def start_http_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logging.info("Healthcheck HTTP server listening on port %s", port)
    server.serve_forever()


# ================== MAIN ==================

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

      # --- Add-rabbit wizard conversation ---
    addrabbit_conv = ConversationHandler(
        entry_points=[CommandHandler("addrabbit", addrabbit_start)],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrabbit_name)],
            ADD_SEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrabbit_sex)],
            ADD_CAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrabbit_cage)],
            ADD_SECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrabbit_section)],
            ADD_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrabbit_weight)],
        },
        fallbacks=[CommandHandler("cancel", addrabbit_cancel)],
    )

    app.add_handler(addrabbit_conv)

    # Core
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("whoami", whoami_cmd))

    # App Menu + Achievements
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("achievements", achievements_cmd))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))

        # Rabbits
    addrabbit_conv = ConversationHandler(
        entry_points=[CommandHandler("addrabbit", addrabbit_start)],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrabbit_name)],
            ADD_SEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrabbit_sex)],
            ADD_CAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrabbit_cage)],
            ADD_SECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrabbit_section)],
            ADD_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrabbit_weight)],
        },
        fallbacks=[CommandHandler("cancel", addrabbit_cancel)],
    )

    app.add_handler(addrabbit_conv)  # /addrabbit wizard
    app.add_handler(CommandHandler("rabbits", rabbits_cmd))
    app.add_handler(CommandHandler("active", active_cmd))
    app.add_handler(CommandHandler("setcage", setcage_cmd))
    app.add_handler(CommandHandler("setparents", setparents_cmd))
    app.add_handler(CommandHandler("checkpair", checkpair_cmd))
    app.add_handler(CommandHandler("markdead", markdead_cmd))


    # Breeding & litters
    app.add_handler(CommandHandler("breed", breed_cmd))
    app.add_handler(CommandHandler("forcebreed", forcebreed_cmd))
    app.add_handler(CommandHandler("kindling", kindling_cmd))
    app.add_handler(CommandHandler("litters", litters_cmd))
    app.add_handler(CommandHandler("littername", littername_cmd))
    app.add_handler(CommandHandler("nextdue", nextdue_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("weaning", weaning_cmd))
    app.add_handler(CommandHandler("suggestbreed", suggestbreed_cmd))

    # Health & weights
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("healthlog", healthlog_cmd))
    app.add_handler(CommandHandler("weight", weight_cmd))
    app.add_handler(CommandHandler("weightlog", weightlog_cmd))
    app.add_handler(CommandHandler("growth", growth_cmd))
    app.add_handler(CommandHandler("growthchart", growthchart_cmd))

    # Money & feed
    app.add_handler(CommandHandler("sell", sell_cmd))
    app.add_handler(CommandHandler("expense", expense_cmd))
    app.add_handler(CommandHandler("electric", electric_cmd))
    app.add_handler(CommandHandler("feed", feed_cmd))
    app.add_handler(CommandHandler("profit", profit_cmd))
    app.add_handler(CommandHandler("profitmonth", profitmonth_cmd))
    app.add_handler(CommandHandler("profityear", profityear_cmd))
    app.add_handler(CommandHandler("feedstats", feedstats_cmd))
    app.add_handler(CommandHandler("feedmonth", feedmonth_cmd))

    # Exports / backup
    app.add_handler(CommandHandler("export_rabbits", export_rabbits_cmd))
    app.add_handler(CommandHandler("export_breedings", export_breedings_cmd))
    app.add_handler(CommandHandler("export_sales", export_sales_cmd))
    app.add_handler(CommandHandler("export_expenses", export_expenses_cmd))
    app.add_handler(CommandHandler("backupdb", backupdb_cmd))

    # Tasks
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("tasklist", tasklist_cmd))
    app.add_handler(CommandHandler("donetask", donetask_cmd))

    # Info & analytics
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("farmsummary", farmsummary_cmd))
    app.add_handler(CommandHandler("tree", tree_cmd))
    app.add_handler(CommandHandler("lineperformance", lineperformance_cmd))
    app.add_handler(CommandHandler("keep", keep_cmd))

    # Climate
    app.add_handler(CommandHandler("settemp", settemp_cmd))
    app.add_handler(CommandHandler("climatealert", climatealert_cmd))

    # Photos
    app.add_handler(CommandHandler("photo", photo_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, photo_upload_handler))

    # Subscribe
    app.add_handler(CommandHandler("subscribe", subscribe_cmd))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))

    return app



def main():
    logging.basicConfig(level=logging.INFO)
    init_db()

    app = build_app()
    app.run_polling()


if __name__ == "__main__":
    # Start tiny HTTP healthcheck server in background so Render sees a port
    threading.Thread(target=start_http_server, daemon=True).start()
    main()


















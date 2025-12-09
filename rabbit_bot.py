import logging
import sqlite3
from datetime import date, timedelta, time
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ================== CONFIG ==================
# Get token from environment (Render Environment -> BOT_TOKEN)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

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
    # NEW: photo support
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

    conn.commit()
    conn.close()


# ================== BASIC RABBIT FUNCS ==================

def add_rabbit(name, sex):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO rabbits(name, sex) VALUES (?, ?)", (name, sex))
        conn.commit()
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


def checkpair_inbreeding(name1, name2):
    r1 = get_rabbit(name1)
    r2 = get_rabbit(name2)
    if not r1 or not r2:
        return "❌ One or both rabbits not found."
    if r1["id"] == r2["id"]:
        return "❌ Same rabbit."

    parents1 = set(x for x in [r1["mother_id"], r1["father_id"]] if x)
    parents2 = set(x for x in [r2["mother_id"], r2["father_id"]] if x)

    if r1["id"] in parents2 or r2["id"] in parents1:
        return "⚠️ High inbreeding: parent–offspring."

    common_parents = parents1 & parents2
    if common_parents:
        names = [get_rabbit_by_id(pid)["name"] for pid in common_parents]
        return f"⚠️ Close relation: shared parent(s) {', '.join(names)}."

    def parents_ids(r):
        return [x for x in [r["mother_id"], r["father_id"]] if x]

    def grandparents_ids(r):
        ids = set()
        for pid in parents_ids(r):
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
        names = [get_rabbit_by_id(gid)["name"] for gid in common_gp]
        return f"⚠️ Related: shared grandparent(s) {', '.join(names)}."

    return "✅ No close relation found (parents/grandparents)."


# ==== PHOTO SUPPORT (NEW) ====

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

    # Photo info
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


# ================== TELEGRAM HANDLERS ==================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐰 Rabbit Farm Bot\n\n"
        "Rabbits:\n"
        "/addrabbit NAME M/F\n"
        "/rabbits\n"
        "/active\n"
        "/setcage NAME CAGE [SECTION]\n"
        "/setparents CHILD MOTHER FATHER\n"
        "/checkpair R1 R2\n"
        "/markdead NAME [REASON]\n"
        "\nBreeding & litters:\n"
        "/breed DOE BUCK\n"
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
        "\nInfo:\n"
        "/info NAME\n"
        "/stats\n"
        "/farmsummary\n"
        "\nPhotos:\n"
        "Send a photo with caption = NAME to assign it\n"
        "/photo NAME (show stored photo)\n"
        "\nAutomation:\n"
        "/subscribe\n"
        "/unsubscribe"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)


# ---- Rabbits ----

async def addrabbit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split()
    if len(parts) < 3:
        await update.message.reply_text("Usage: /addrabbit NAME M/F")
        return
    name = parts[1]
    sex = parts[2].upper()
    if sex not in ("M", "F"):
        await update.message.reply_text("Sex must be M or F.")
        return
    ok = add_rabbit(name, sex)
    if ok:
        await update.message.reply_text(f"✅ Rabbit {name} ({sex}) added.")
    else:
        await update.message.reply_text("❌ A rabbit with that name already exists.")


async def rabbits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_rabbits(active_only=False)
    if not rows:
        await update.message.reply_text("No rabbits in database.")
        return
    lines = []
    for r in rows:
        lines.append(f"{r['name']} ({r['sex']}) - status: {r['status']}")
    await update.message.reply_text("🐰 All rabbits:\n" + "\n".join(lines))


async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_rabbits(active_only=True)
    if not rows:
        await update.message.reply_text("No active rabbits.")
        return
    lines = [f"{r['name']} ({r['sex']})" for r in rows]
    await update.message.reply_text("🐰 Active rabbits:\n" + "\n".join(lines))


async def setcage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    parts = update.message.text.split()
    if len(parts) < 4:
        await update.message.reply_text("Usage: /setparents CHILD MOTHER FATHER")
        return
    child, mother, father = parts[1], parts[2], parts[3]
    msg = update_rabbit_parents(child, mother, father)
    await update.message.reply_text(msg)


async def checkpair_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split()
    if len(parts) < 3:
        await update.message.reply_text("Usage: /checkpair RABBIT1 RABBIT2")
        return
    msg = checkpair_inbreeding(parts[1], parts[2])
    await update.message.reply_text(msg)


async def markdead_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    parts = update.message.text.split()
    if len(parts) < 3:
        await update.message.reply_text("Usage: /breed DOE BUCK")
        return
    doe, buck = parts[1], parts[2]
    msg = add_breeding(doe, buck)
    await update.message.reply_text(msg)


async def kindling_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /littername DOE LITTERNAME")
        return
    doe, ln = parts[1], parts[2]
    msg = set_litter_name_for_latest(doe, ln)
    await update.message.reply_text(msg)


async def nextdue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    dues = get_due_today()
    tasks = get_tasks_for_date(date.today())

    lines = []
    if dues:
        lines.append("🍼 Kindlings due today:")
        lines.extend([f"- {r['name']}" for r in dues])
    else:
        lines.append("No kindlings due today.")

    if tasks:
        lines.append("\n📌 Tasks for today:")
        for t in tasks:
            line = f"- #{t['id']} [{t['task_date']}] {t['title']}"
            if t["note"]:
                line += f" – {t['note']}"
            lines.append(line)
    else:
        lines.append("\nNo tasks for today.")

    await update.message.reply_text("\n".join(lines))


async def weaning_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_weaning_today()
    if not rows:
        await update.message.reply_text("No weaning scheduled for today.")
        return
    lines = [f"- {r['name']}" for r in rows]
    await update.message.reply_text("🐇 Weaning today for:\n" + "\n".join(lines))


# ---- Health & weights ----

async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Usage: /health NAME note...")
        return
    name = parts[1]
    note = parts[2]
    msg = add_health_record(name, note)
    await update.message.reply_text(msg)


async def healthlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


# ---- Money & feed ----

async def sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    inc, exp, prof = get_profit_summary(None)
    await update.message.reply_text(
        f"💰 Profit (all time):\nIncome: {inc}\nExpenses: {exp}\nProfit: {prof}"
    )


async def profitmonth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    kg, cost = get_feed_stats(None)
    await update.message.reply_text(
        f"🌾 Feed stats (all time):\nTotal feed: {kg} kg\nCost: {cost}"
    )


async def feedmonth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /feedmonth YYYY-MM")
        return
    period = parts[1]
    kg, cost = get_feed_stats(period)
    await update.message.reply_text(
        f"🌾 Feed stats for {period}:\nTotal feed: {kg} kg\nCost: {cost}"
    )


# ---- Tasks ----

async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


# ---- Info ----

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /info NAME")
        return
    msg = get_info_message(parts[1])
    await update.message.reply_text(msg)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = get_stats_message()
    await update.message.reply_text(msg)


async def farmsummary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = get_farmsummary_message()
    await update.message.reply_text(msg)


# ---- Photos (NEW) ----

async def photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    # Use first word of caption as rabbit name
    name = caption.split()[0]
    photo = update.message.photo[-1]  # highest resolution
    file_id = photo.file_id

    ok, msg = set_rabbit_photo(name, file_id)
    await update.message.reply_text(msg)


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

    try:
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
    except Exception as e:
        logging.error("Error in daily_job: %s", e)


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


def start_http_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logging.info("Healthcheck HTTP server listening on port %s", port)
    server.serve_forever()


# ================== MAIN ==================

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # Rabbits
    app.add_handler(CommandHandler("addrabbit", addrabbit_cmd))
    app.add_handler(CommandHandler("rabbits", rabbits_cmd))
    app.add_handler(CommandHandler("active", active_cmd))
    app.add_handler(CommandHandler("setcage", setcage_cmd))
    app.add_handler(CommandHandler("setparents", setparents_cmd))
    app.add_handler(CommandHandler("checkpair", checkpair_cmd))
    app.add_handler(CommandHandler("markdead", markdead_cmd))

    # Breeding & litters
    app.add_handler(CommandHandler("breed", breed_cmd))
    app.add_handler(CommandHandler("kindling", kindling_cmd))
    app.add_handler(CommandHandler("litters", litters_cmd))
    app.add_handler(CommandHandler("littername", littername_cmd))
    app.add_handler(CommandHandler("nextdue", nextdue_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("weaning", weaning_cmd))

    # Health & weights
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("healthlog", healthlog_cmd))
    app.add_handler(CommandHandler("weight", weight_cmd))
    app.add_handler(CommandHandler("weightlog", weightlog_cmd))

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

    # Tasks
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("tasklist", tasklist_cmd))
    app.add_handler(CommandHandler("donetask", donetask_cmd))

    # Info
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("farmsummary", farmsummary_cmd))

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

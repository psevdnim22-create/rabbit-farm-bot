import logging
import sqlite3
from datetime import date, timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================== CONFIG ==================
BOT_TOKEN = "8567471850:AAEtQPkyjyTtjJtpw0H8sw7AvPgC3WYGCHE"  # <<< PUT YOUR REAL TOKEN HERE
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
        # column already exists
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
            FOREIGN KEY (doe_id) REFERENCES rabbits(id),
            FOREIGN KEY (buck_id) REFERENCES rabbits(id)
        )
    """)
    safe_alter(cur, "ALTER TABLE breedings ADD COLUMN litter_name TEXT")

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
    cur.execute(
        "UPDATE rabbits SET mother_id=?, father_id=? WHERE id=?",
        (mother["id"], father["id"], child["id"]),
    )
    conn.commit()
    conn.close()
    return f"✅ Parents set for {child_name}: mother {mother_name}, father {father_name}."


def set_cage_section(name, cage, section=None):
    r = get_rabbit(name)
    if not r:
        return "❌ Rabbit not found."
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE rabbits SET cage=?, section=? WHERE id=?",
        (cage, section, r["id"]),
    )
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
    cur.execute(
        """
        UPDATE rabbits
        SET status='dead', death_date=?, death_reason=?
        WHERE id=?
        """,
        (today_str, reason, r["id"]),
    )
    conn.commit()
    conn.close()
    msg = f"☠️ {name} marked as dead."
    if reason:
        msg += f" Reason: {reason}"
    return msg


def checkpair_inbreeding(name1, name2):
    r1 = get_rabbit(name1)
    r2 = get_rabbit(name2)
    if not r1 or not r2:
        return "❌ One or both rabbits not found."
    if r1["id"] == r2["id"]:
        return "❌ Same rabbit, cannot breed."

    parents1 = set(x for x in [r1["mother_id"], r1["father_id"]] if x)
    parents2 = set(x for x in [r2["mother_id"], r2["father_id"]] if x)

    # parent-child
    if r1["id"] in parents2 or r2["id"] in parents1:
        return "⚠️ High inbreeding: parent–offspring mating."

    # siblings / half-siblings
    common_parents = parents1 & parents2
    if common_parents:
        names = [get_rabbit_by_id(pid)["name"] for pid in common_parents]
        return f"⚠️ Close relation: shared parent(s): {', '.join(names)}."

    # grandparents
    def get_parents_ids(r):
        return [x for x in [r["mother_id"], r["father_id"]] if x]

    def get_grandparents_ids(r):
        ids = set()
        for pid in get_parents_ids(r):
            pr = get_rabbit_by_id(pid)
            if pr:
                for g in [pr["mother_id"], pr["father_id"]]:
                    if g:
                        ids.add(g)
        return ids

    gp1 = get_grandparents_ids(r1)
    gp2 = get_grandparents_ids(r2)
    common_gp = gp1 & gp2
    if common_gp:
        names = [get_rabbit_by_id(gid)["name"] for gid in common_gp]
        return f"⚠️ Related: shared grandparent(s): {', '.join(names)}."

    return "✅ No close relation found (up to parents & grandparents)."


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
    cur.execute(
        """
        INSERT INTO breedings(doe_id, buck_id, mating_date, expected_due_date)
        VALUES (?, ?, ?, ?)
        """,
        (doe["id"], buck["id"], mating.strftime("%Y-%m-%d"), due.strftime("%Y-%m-%d")),
    )
    conn.commit()
    conn.close()

    return f"✅ {doe_name} bred with {buck_name}\nDue date: {due}"


def record_kindling(doe_name, litter_size, litter_name=None):
    doe = get_rabbit(doe_name)
    if not doe:
        return "❌ Doe not found."

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM breedings
        WHERE doe_id=? AND kindling_date IS NULL
        ORDER BY DATE(mating_date) DESC
        LIMIT 1
        """,
        (doe["id"],),
    )
    breeding = cur.fetchone()

    if not breeding:
        conn.close()
        return "❌ No open breeding found for this doe."

    kindling = date.today()
    weaning = kindling + timedelta(days=WEANING_DAYS)

    if litter_name:
        cur.execute(
            """
            UPDATE breedings
            SET kindling_date=?, litter_size=?, weaning_date=?, litter_name=?
            WHERE id=?
            """,
            (
                kindling.strftime("%Y-%m-%d"),
                litter_size,
                weaning.strftime("%Y-%m-%d"),
                litter_name,
                breeding["id"],
            ),
        )
    else:
        cur.execute(
            """
            UPDATE breedings
            SET kindling_date=?, litter_size=?, weaning_date=?
            WHERE id=?
            """,
            (
                kindling.strftime("%Y-%m-%d"),
                litter_size,
                weaning.strftime("%Y-%m-%d"),
                breeding["id"],
            ),
        )

    conn.commit()
    conn.close()

    msg = f"🍼 Kindling recorded for {doe_name}\nKits: {litter_size}\nWeaning date: {weaning}"
    if litter_name:
        msg += f"\nLitter name: {litter_name}"
    return msg


def get_due_today():
    today = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.name
        FROM breedings b
        JOIN rabbits r ON r.id=b.doe_id
        WHERE b.expected_due_date=?
        """,
        (today,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_weaning_today():
    today = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.name
        FROM breedings b
        JOIN rabbits r ON r.id=b.doe_id
        WHERE b.weaning_date=?
        """,
        (today,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_litters_for_doe(doe_name):
    doe = get_rabbit(doe_name)
    if not doe:
        return None, []

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
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
        """,
        (doe["id"],),
    )
    rows = cur.fetchall()
    conn.close()
    return doe, rows


def set_litter_name_for_latest(doe_name, litter_name):
    doe = get_rabbit(doe_name)
    if not doe:
        return "❌ Doe not found."

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id FROM breedings
        WHERE doe_id = ? AND kindling_date IS NOT NULL
        ORDER BY DATE(kindling_date) DESC, DATE(mating_date) DESC
        LIMIT 1
        """,
        (doe["id"],),
    )
    row = cur.fetchone()

    if not row:
        conn.close()
        return "❌ No litters found for this doe."

    cur.execute("UPDATE breedings SET litter_name=? WHERE id=?", (litter_name, row["id"]))
    conn.commit()
    conn.close()
    return f"✅ Litter name for {doe_name}'s last litter set to: {litter_name}."


def get_next_due_for_doe(doe_name):
    doe = get_rabbit(doe_name)
    if not doe:
        return None

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM breedings
        WHERE doe_id=? AND kindling_date IS NULL
        ORDER BY DATE(expected_due_date) ASC
        LIMIT 1
        """,
        (doe["id"],),
    )
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
    cur.execute(
        """
        INSERT INTO health_records(rabbit_id, record_date, note)
        VALUES (?, ?, ?)
        """,
        (rabbit["id"], today_str, note),
    )
    conn.commit()
    conn.close()
    return f"✅ Health note added for {name}."


def get_health_log(name, limit=5):
    rabbit = get_rabbit(name)
    if not rabbit:
        return None, []
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT record_date, note
        FROM health_records
        WHERE rabbit_id = ?
        ORDER BY record_date DESC, id DESC
        LIMIT ?
        """,
        (rabbit["id"], limit),
    )
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
    cur.execute(
        """
        INSERT INTO sales(rabbit_id, sale_date, price, buyer)
        VALUES (?, ?, ?, ?)
        """,
        (rabbit["id"], today_str, price, buyer),
    )
    cur.execute("UPDATE rabbits SET status='sold' WHERE id=?", (rabbit["id"],))
    conn.commit()
    conn.close()

    extra = ""
    if price is not None:
        extra += f" for {price}"
    if buyer:
        extra += f" to {buyer}"
    return f"💸 Sale recorded: {name}{extra}."


def add_weight(name, weight_kg):
    rabbit = get_rabbit(name)
    if not rabbit:
        return "❌ Rabbit not found."
    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO weights(rabbit_id, weigh_date, weight_kg)
        VALUES (?, ?, ?)
        """,
        (rabbit["id"], today_str, weight_kg),
    )
    conn.commit()
    conn.close()
    return f"✅ Weight recorded for {name}: {weight_kg} kg."


def get_weight_log(name, limit=5):
    rabbit = get_rabbit(name)
    if not rabbit:
        return None, []
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT weigh_date, weight_kg
        FROM weights
        WHERE rabbit_id = ?
        ORDER BY weigh_date DESC, id DESC
        LIMIT ?
        """,
        (rabbit["id"], limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rabbit, rows


# ================== EXPENSES, FEED, PROFIT ==================

def add_expense(amount, category, note=None):
    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO expenses(exp_date, category, amount, note)
        VALUES (?, ?, ?, ?)
        """,
        (today_str, category, amount, note),
    )
    conn.commit()
    conn.close()
    return f"✅ Expense recorded: {amount} ({category})."


def add_feed(amount_kg, cost, note=None):
    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO feed_logs(log_date, amount_kg, cost, note)
        VALUES (?, ?, ?, ?)
        """,
        (today_str, amount_kg, cost, note),
    )
    conn.commit()
    conn.close()
    return f"✅ Feed log: {amount_kg} kg, cost {cost}."


def get_profit_summary(period=None):
    """
    period:
      None       -> all time
      'YYYY-MM'  -> month
      'YYYY'     -> year
    """
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

    cur.execute(
        f"SELECT COALESCE(SUM(price),0) AS s FROM sales {sales_where}",
        params_sales,
    )
    income = cur.fetchone()["s"]

    cur.execute(
        f"SELECT COALESCE(SUM(amount),0) AS e FROM expenses {exp_where}",
        params_exp,
    )
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

    cur.execute(
        f"""
        SELECT COALESCE(SUM(amount_kg),0) AS kg,
               COALESCE(SUM(cost),0)      AS c
        FROM feed_logs {where}
        """,
        params,
    )
    row = cur.fetchone()
    conn.close()
    return row["kg"], row["c"]


# ================== TASKS ==================

def add_task(task_date_str, title, note=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO tasks(task_date, title, note)
        VALUES (?, ?, ?)
        """,
        (task_date_str, title, note),
    )
    conn.commit()
    conn.close()
    return "✅ Task added."


def get_tasks_for_date(d):
    ds = d.strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM tasks
        WHERE task_date=? AND done=0
        ORDER BY id
        """,
        (ds,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_upcoming_tasks(limit=10):
    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM tasks
        WHERE task_date>=? AND done=0
        ORDER BY task_date, id
        LIMIT ?
        """,
        (today_str, limit),
    )
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

    cur.execute(
        "SELECT COALESCE(SUM(litter_size), 0) AS s "
        "FROM breedings WHERE litter_size IS NOT NULL"
    )
    total_kits = cur.fetchone()["s"]

    cur.execute("SELECT COUNT(*) AS c FROM sales")
    total_sales = cur.fetchone()["c"]

    conn.close()

    msg = "📊 Farm stats:\n"
    msg += (
        f"- Rabbits: {total_rabbits} "
        f"(Active: {active_rabbits}, Does: {total_does}, Bucks: {total_bucks})\n"
    )
    msg += f"- Breedings: {total_breedings}\n"
    msg += f"- Litters recorded: {total_litters}\n"
    msg += (
        f"- Kits recorded: {int(total_kits) if total_kits is not None else 0}\n"
    )
    msg += f"- Sales recorded: {total_sales}\n"
    return msg


def get_info_message(name):
    r = get_rabbit(name)
    if not r:
        return "❌ Rabbit not found."

    lines = [f"🐰 {r['name']} ({'Doe' if r['sex']=='F' else 'Buck'})"]

    # status
    lines.append(f"Status: {r['status'] or 'unknown'}")
    if r["status"] == "dead":
        if r["death_date"]:
            lines.append(f"  Died: {r['death_date']}")
        if r["death_reason"]:
            lines.append(f"  Reason: {r['death_reason']}")

    # location
    if r["cage"] or r["section"]:
        loc = []
        if r["cage"]:
            loc.append(f"cage {r['cage']}")
        if r["section"]:
            loc.append(f"section {r['section']}")
        lines.append("Location: " + ", ".join(loc))

    # parents
    mother = get_rabbit_by_id(r["mother_id"])
    father = get_rabbit_by_id(r["father_id"])
    if mother or father:
        m_name = mother["name"] if mother else "unknown"
        f_name = father["name"] if father else "unknown"
        lines.append(f"Parents: {m_name} × {f_name}")

    # litters (if doe)
    if r["sex"] == "F":
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) AS c, COALESCE(SUM(litter_size),0) AS s
            FROM breedings
            WHERE doe_id=? AND kindling_date IS NOT NULL
            """,
            (r["id"],),
        )
        row = cur.fetchone()
        litters = row["c"]
        kits = int(row["s"])
        cur.execute(
            """
            SELECT * FROM breedings
            WHERE doe_id=? AND kindling_date IS NOT NULL
            ORDER BY DATE(kindling_date) DESC
            LIMIT 1
            """,
            (r["id"],),
        )
        last = cur.fetchone()
        conn.close()

        lines.append(f"Litters: {litters} (total kits: {kits})")
        if last:
            ln = last["litter_name"] or "(no name)"
            lines.append(
                f"Last litter: {ln}, kindled {last['kindling_date']}, "
                f"{last['litter_size']} kits"
            )

        # next due
        nxt = get_next_due_for_doe(name)
        if nxt:
            lines.append(
                f"Next due: {nxt['expected_due_date']} "
                f"(bred on {nxt['mating_date']})"
            )

    # latest health
    rabbit, h_records = get_health_log(name, limit=1)
    if rabbit and h_records:
        lines.append(
            f"Last health note: {h_records[0]['record_date']} – "
            f"{h_records[0]['note']}"
        )

    # latest sale
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM sales
        WHERE rabbit_id=?
        ORDER BY sale_date DESC, id DESC
        LIMIT 1
        """,
        (r["id"],),
    )
    s = cur.fetchone()
    conn.close()
    if s:
        lines.append(
            f"Last sale: {s['sale_date']} for {s['price']} "
            f"to {s['buyer'] or 'unknown buyer'}"
        )

    return "\n".join(lines)


def get_farmsummary_message():
    stats = get_stats_message()
    income_all, exp_all, prof_all = get_profit_summary(period=None)
    feed_kg, feed_cost = get_feed_stats(period=None)

    msg = stats + "\n\n💰 Financial (all time):\n"
    msg += f"- Income: {income_all}\n"
    msg += f"- Expenses: {exp_all}\n"
    msg += f"- Profit: {prof_all}\n"

    msg += "\n🌾 Feed (all time):\n"
    msg += f"- Total feed: {feed_kg} kg\n"
    msg += f"- Feed cost: {feed_cost}\n"
    return msg


# ================== TELEGRAM HANDLERS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐰 Rabbit Farm Bot\n\n"
        "Rabbits:\n"
        "/addrabbit NAME M/F\n"
        "/rabbits – list all\n"
        "/active – list active\n"
        "/setcage NAME CAGE [SECTION]\n"
        "/setparents CHILD MOTHER FATHER\n"
        "/checkpair R1 R2 – inbreeding check\n"
        "/markdead NAME [REASON]\n"
        "\nBreeding & litters:\n"
        "/breed DOE BUCK\n"
        "/kindling DOE LITTER_SIZE [LITTERNAME]\n"
        "/litters DOE\n"
        "/littername DOE LITTERNAME\n"
        "/nextdue DOE\n"
        "/today – due + weaning + tasks\n"
        "/weaning – weaning today\n"
        "\nHealth & weights:\n"
        "/health NAME NOTE\n"
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
        "\nInfo & summary:\n"
        "/info NAME\n"
        "/stats\n"
        "/farmsummary\n"
        "\nNotifications:\n"
        "/subscribe – daily summary ON\n"
        "/unsubscribe – daily summary OFF"
    )


# --- core commands ---

async def addrabbit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addrabbit Bella F")
        return
    name = context.args[0]
    sex = context.args[1].upper()
    if sex not in ("M", "F"):
        await update.message.reply_text("Sex must be M or F.")
        return
    if add_rabbit(name, sex):
        await update.message.reply_text("✅ Rabbit added.")
    else:
        await update.message.reply_text("⚠️ Rabbit with that name already exists.")


async def rabbits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rabbits = list_rabbits(active_only=False)
    if not rabbits:
        await update.message.reply_text("No rabbits recorded yet.")
        return
    msg = "🐇 Rabbits:\n"
    for r in rabbits:
        msg += f"- {r['name']} ({'Doe' if r['sex']=='F' else 'Buck'}, {r['status']})\n"
    await update.message.reply_text(msg)


async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rabbits = list_rabbits(active_only=True)
    if not rabbits:
        await update.message.reply_text("No active rabbits.")
        return
    msg = "🐇 Active rabbits:\n"
    for r in rabbits:
        msg += f"- {r['name']} ({'Doe' if r['sex']=='F' else 'Buck'})\n"
    await update.message.reply_text(msg)


async def setcage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setcage NAME CAGE [SECTION]")
        return
    name = context.args[0]
    cage = context.args[1]
    section = context.args[2] if len(context.args) >= 3 else None
    msg = set_cage_section(name, cage, section)
    await update.message.reply_text(msg)


async def setparents_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /setparents CHILD MOTHER FATHER")
        return
    child, mother, father = context.args[0], context.args[1], context.args[2]
    msg = update_rabbit_parents(child, mother, father)
    await update.message.reply_text(msg)


async def checkpair_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /checkpair RABBIT1 RABBIT2")
        return
    msg = checkpair_inbreeding(context.args[0], context.args[1])
    await update.message.reply_text(msg)


async def markdead_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /markdead NAME [REASON]")
        return
    name = context.args[0]
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else None
    msg = mark_dead(name, reason)
    await update.message.reply_text(msg)


# --- breeding / litters ---

async def breed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /breed DoeName BuckName")
        return
    msg = add_breeding(context.args[0], context.args[1])
    await update.message.reply_text(msg)


async def kindling_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /kindling DoeName LitterSize [LitterName]")
        return
    doe_name = context.args[0]
    try:
        count = int(context.args[1])
    except ValueError:
        await update.message.reply_text("LitterSize must be a number.")
        return
    litter_name = context.args[2] if len(context.args) >= 3 else None
    msg = record_kindling(doe_name, count, litter_name)
    await update.message.reply_text(msg)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    due = get_due_today()
    weaning = get_weaning_today()
    tasks = get_tasks_for_date(date.today())

    msg = "📅 Today's overview:\n"
    if due:
        msg += "\n🍼 Does due today:\n"
        for r in due:
            msg += f"- {r['name']}\n"
    if weaning:
        msg += "\n🚼 Weaning today:\n"
        for r in weaning:
            msg += f"- {r['name']}\n"
    if tasks:
        msg += "\n🧹 Tasks:\n"
        for t in tasks:
            msg += f"- #{t['id']} {t['task_date']}: {t['title']}\n"
    if not due and not weaning and not tasks:
        msg += "Nothing scheduled today 🐰"
    await update.message.reply_text(msg)


async def weaning_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_weaning_today()
    if not rows:
        await update.message.reply_text("No litters to wean today.")
        return
    msg = "🚼 Weaning today:\n"
    for r in rows:
        msg += f"- {r['name']}\n"
    await update.message.reply_text(msg)


async def litters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /litters DoeName")
        return
    doe_name = context.args[0]
    doe, rows = get_litters_for_doe(doe_name)
    if doe is None:
        await update.message.reply_text("❌ Doe not found.")
        return
    if not rows:
        await update.message.reply_text(f"No litters recorded for {doe_name}.")
        return
    msg = f"🧺 Litters for {doe_name}:\n"
    for r in rows:
        name = r["litter_name"] if r["litter_name"] else "(no name)"
        msg += (
            f"- {name}: buck {r['buck_name']}, "
            f"mated {r['mating_date']}, kindled {r['kindling_date']}, "
            f"{r['litter_size']} kits\n"
        )
    await update.message.reply_text(msg)


async def littername_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /littername DoeName LitterName")
        return
    msg = set_litter_name_for_latest(context.args[0], context.args[1])
    await update.message.reply_text(msg)


async def nextdue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /nextdue DoeName")
        return
    doe_name = context.args[0]
    nxt = get_next_due_for_doe(doe_name)
    if not nxt:
        await update.message.reply_text(f"No open breedings found for {doe_name}.")
        return
    msg = (
        f"{doe_name} is due on {nxt['expected_due_date']} "
        f"(bred on {nxt['mating_date']})."
    )
    await update.message.reply_text(msg)


# --- health / weights ---

async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /health Name note...")
        return
    name = context.args[0]
    note = " ".join(context.args[1:])
    msg = add_health_record(name, note)
    await update.message.reply_text(msg)


async def healthlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /healthlog Name")
        return
    name = context.args[0]
    rabbit, records = get_health_log(name)
    if rabbit is None:
        await update.message.reply_text("❌ Rabbit not found.")
        return
    if not records:
        await update.message.reply_text(f"No health records for {name}.")
        return
    msg = f"📝 Health log for {name}:\n"
    for r in records:
        msg += f"- {r['record_date']}: {r['note']}\n"
    await update.message.reply_text(msg)


async def weight_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /weight Name KG")
        return
    name = context.args[0]
    try:
        kg = float(context.args[1])
    except ValueError:
        await update.message.reply_text("KG must be a number.")
        return
    msg = add_weight(name, kg)
    await update.message.reply_text(msg)


async def weightlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /weightlog Name")
        return
    name = context.args[0]
    rabbit, records = get_weight_log(name)
    if rabbit is None:
        await update.message.reply_text("❌ Rabbit not found.")
        return
    if not records:
        await update.message.reply_text(f"No weight records for {name}.")
        return
    msg = f"⚖️ Weight log for {name}:\n"
    for r in records:
        msg += f"- {r['weigh_date']}: {r['weight_kg']} kg\n"
    await update.message.reply_text(msg)


# --- money / feed ---

async def sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /sell Name Price [Buyer]")
        return
    name = context.args[0]
    try:
        price = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Price must be a number.")
        return
    buyer = " ".join(context.args[2:]) if len(context.args) > 2 else None
    msg = record_sale(name, price, buyer)
    await update.message.reply_text(msg)


async def expense_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /expense Amount Category [Note]")
        return
    try:
        amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    category = context.args[1]
    note = " ".join(context.args[2:]) if len(context.args) > 2 else None
    msg = add_expense(amount, category, note)
    await update.message.reply_text(msg)


async def electric_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /electric Amount [Note]")
        return
    try:
        amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    note = " ".join(context.args[1:]) if len(context.args) > 1 else None
    msg = add_expense(amount, "electricity", note)
    await update.message.reply_text(msg)


async def feed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /feed KG Cost [Note]")
        return
    try:
        kg = float(context.args[0])
        cost = float(context.args[1])
    except ValueError:
        await update.message.reply_text("KG and Cost must be numbers.")
        return
    note = " ".join(context.args[2:]) if len(context.args) > 2 else None
    msg = add_feed(kg, cost, note)
    await update.message.reply_text(msg)


async def profit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    income, expenses, profit = get_profit_summary()
    msg = "💰 Profit (all time):\n"
    msg += f"- Income: {income}\n"
    msg += f"- Expenses: {expenses}\n"
    msg += f"- Profit: {profit}\n"
    await update.message.reply_text(msg)


async def profitmonth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /profitmonth YYYY-MM")
        return
    period = context.args[0]
    income, expenses, profit = get_profit_summary(period)
    msg = f"💰 Profit for {period}:\n"
    msg += f"- Income: {income}\n"
    msg += f"- Expenses: {expenses}\n"
    msg += f"- Profit: {profit}\n"
    await update.message.reply_text(msg)


async def profityear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /profityear YYYY")
        return
    period = context.args[0]
    income, expenses, profit = get_profit_summary(period)
    msg = f"💰 Profit for {period}:\n"
    msg += f"- Income: {income}\n"
    msg += f"- Expenses: {expenses}\n"
    msg += f"- Profit: {profit}\n"
    await update.message.reply_text(msg)


async def feedstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kg, cost = get_feed_stats()
    msg = "🌾 Feed stats (all time):\n"
    msg += f"- Total feed used: {kg} kg\n"
    msg += f"- Total feed cost: {cost}\n"
    await update.message.reply_text(msg)


async def feedmonth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /feedmonth YYYY-MM")
        return
    period = context.args[0]
    kg, cost = get_feed_stats(period)
    msg = f"🌾 Feed stats for {period}:\n"
    msg += f"- Total feed used: {kg} kg\n"
    msg += f"- Total feed cost: {cost}\n"
    await update.message.reply_text(msg)


# --- tasks / reminders ---

async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /remind YYYY-MM-DD Text")
        return
    date_str = context.args[0]
    title = " ".join(context.args[1:])
    msg = add_task(date_str, title)
    await update.message.reply_text(msg)


async def tasklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_upcoming_tasks()
    if not tasks:
        await update.message.reply_text("No upcoming tasks.")
        return
    msg = "🧹 Upcoming tasks:\n"
    for t in tasks:
        msg += f"- #{t['id']} {t['task_date']}: {t['title']}\n"
    await update.message.reply_text(msg)


async def donetask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /donetask ID")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return
    if mark_task_done(tid):
        await update.message.reply_text(f"✅ Task #{tid} marked as done.")
    else:
        await update.message.reply_text(f"❌ Task #{tid} not found.")


# --- stats / info ---

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = get_stats_message()
    await update.message.reply_text(msg)


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /info Name")
        return
    msg = get_info_message(context.args[0])
    await update.message.reply_text(msg)


async def farmsummary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = get_farmsummary_message()
    await update.message.reply_text(msg)


# --- daily reminders --- 

async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    due = get_due_today()
    weaning = get_weaning_today()
    tasks = get_tasks_for_date(date.today())

    msg = "📅 Daily farm update:\n"
    if due:
        msg += "\n🍼 Due today:\n"
        for r in due:
            msg += f"- {r['name']}\n"
    if weaning:
        msg += "\n🚼 Weaning today:\n"
        for r in weaning:
            msg += f"- {r['name']}\n"
    if tasks:
        msg += "\n🧹 Tasks:\n"
        for t in tasks:
            msg += f"- #{t['id']} {t['title']}\n"
    if not due and not weaning and not tasks:
        msg += "Nothing scheduled today 🐰"

    await context.bot.send_message(chat_id=chat_id, text=msg)


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    job_name = f"daily_{chat_id}"
    existing = context.job_queue.get_jobs_by_name(job_name)
    if existing:
        await update.message.reply_text("You are already subscribed.")
        return
    context.job_queue.run_repeating(
        daily_job,
        interval=24 * 60 * 60,
        first=0,
        chat_id=chat_id,
        name=job_name,
    )
    await update.message.reply_text("✅ Daily farm summary enabled.")


async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    job_name = f"daily_{chat_id}"
    jobs = context.job_queue.get_jobs_by_name(job_name)
    if not jobs:
        await update.message.reply_text("You are not subscribed.")
        return
    for job in jobs:
        job.schedule_removal()
    await update.message.reply_text("❌ Daily farm summary disabled.")


# ================== APP & MAIN ==================

def build_app() -> Application:
    init_db()
    logging.basicConfig(level=logging.INFO)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))

    app.add_handler(CommandHandler("addrabbit", addrabbit_cmd))
    app.add_handler(CommandHandler("rabbits", rabbits_cmd))
    app.add_handler(CommandHandler("active", active_cmd))
    app.add_handler(CommandHandler("setcage", setcage_cmd))
    app.add_handler(CommandHandler("setparents", setparents_cmd))
    app.add_handler(CommandHandler("checkpair", checkpair_cmd))
    app.add_handler(CommandHandler("markdead", markdead_cmd))

    app.add_handler(CommandHandler("breed", breed_cmd))
    app.add_handler(CommandHandler("kindling", kindling_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("weaning", weaning_cmd))
    app.add_handler(CommandHandler("litters", litters_cmd))
    app.add_handler(CommandHandler("littername", littername_cmd))
    app.add_handler(CommandHandler("nextdue", nextdue_cmd))

    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("healthlog", healthlog_cmd))
    app.add_handler(CommandHandler("weight", weight_cmd))
    app.add_handler(CommandHandler("weightlog", weightlog_cmd))

    app.add_handler(CommandHandler("sell", sell_cmd))
    app.add_handler(CommandHandler("expense", expense_cmd))
    app.add_handler(CommandHandler("electric", electric_cmd))
    app.add_handler(CommandHandler("feed", feed_cmd))
    app.add_handler(CommandHandler("profit", profit_cmd))
    app.add_handler(CommandHandler("profitmonth", profitmonth_cmd))
    app.add_handler(CommandHandler("profityear", profityear_cmd))
    app.add_handler(CommandHandler("feedstats", feedstats_cmd))
    app.add_handler(CommandHandler("feedmonth", feedmonth_cmd))

    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("tasklist", tasklist_cmd))
    app.add_handler(CommandHandler("donetask", donetask_cmd))

    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("farmsummary", farmsummary_cmd))

    app.add_handler(CommandHandler("subscribe", subscribe_cmd))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))

    return app


def main():
    app = build_app()
    app.run_polling()


if __name__ == "__main__":
    main()



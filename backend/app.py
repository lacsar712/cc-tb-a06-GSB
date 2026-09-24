import os
from functools import wraps

import psycopg2
from flask import Flask, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}

CUPPING_COLUMNS = """
    c.*, q.id AS queue_id, q.printed AS queue_printed
    FROM cuppings c
    LEFT JOIN print_queue q ON q.cupping_id = c.id
"""


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


def writer_required(fn):
    @wraps(fn)
    @login_required
    def wrap(*args, **kwargs):
        if session.get("role") != "writer":
            return ("仅审评员可操作", 403)
        return fn(*args, **kwargs)

    return wrap


def parse_scores(form):
    try:
        aroma = float(form["aroma"])
        taste = float(form["taste"])
        liquor = float(form["liquor"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(0 <= v <= 10 for v in (aroma, taste, liquor)):
        return None
    return aroma, taste, liquor


def get_cupping(cur, cupping_id):
    cur.execute(f"SELECT {CUPPING_COLUMNS} WHERE c.id = %s", (cupping_id,))
    return cur.fetchone()


@app.get("/health")
def health():
    return {"status": "ok", "service": "tea-blend-cupping"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        account = ACCOUNTS.get(name)
        if not account or account["password"] != request.form.get("password", ""):
            error = "用户名或密码错误"
        else:
            session["user"] = name
            session["role"] = account["role"]
            return redirect(url_for("home"))
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT {CUPPING_COLUMNS} ORDER BY c.id DESC")
        rows = cur.fetchall()
    return render_template("home.html", rows=rows, can_write=session.get("role") == "writer")


@app.post("/cuppings")
@writer_required
def create():
    scores = parse_scores(request.form)
    if scores is None:
        return ("三项分须为 0 到 10 的数字", 400)
    aroma, taste, liquor = scores
    lot = request.form.get("lot", "").strip()
    if not lot:
        return ("批次不能为空", 400)
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"]),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        row = get_cupping(cur, new_id)
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row, can_write=True)
    return redirect(url_for("home"))


@app.post("/cuppings/<int:cupping_id>/correct")
@writer_required
def correct(cupping_id):
    """审评员改正三项分并重算结论；已入队的冻结正文不受影响。"""
    scores = parse_scores(request.form)
    if scores is None:
        return ("三项分须为 0 到 10 的数字", 400)
    aroma, taste, liquor = scores
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """UPDATE cuppings
               SET aroma = %s, taste = %s, liquor = %s, score = %s, verdict = %s, note = %s
               WHERE id = %s RETURNING id""",
            (aroma, taste, liquor, score, verdict, note, cupping_id),
        )
        if cur.fetchone() is None:
            return ("审评记录不存在", 404)
        conn.commit()
        row = get_cupping(cur, cupping_id)
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row, can_write=True)
    return redirect(url_for("home"))


@app.post("/cuppings/<int:cupping_id>/enqueue")
@writer_required
def enqueue(cupping_id):
    """把通过行排入出样打印队列；入队时冻结当时的批次、三项分、加权分与结论原文。"""
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings WHERE id = %s", (cupping_id,))
        row = cur.fetchone()
        if row is None:
            return ("审评记录不存在", 404)
        if row["verdict"] != "通过":
            return ("未通过行禁止入队", 400)
        cur.execute("SELECT 1 FROM print_queue WHERE cupping_id = %s", (cupping_id,))
        if cur.fetchone() is not None:
            return ("该行已在出样队列中", 409)
        try:
            cur.execute(
                """INSERT INTO print_queue
                       (cupping_id, lot, aroma, taste, liquor, score, verdict, note, enqueued_by)
                   SELECT id, lot, aroma, taste, liquor, score, verdict, note, %s
                   FROM cuppings WHERE id = %s
                   RETURNING id""",
                (session["user"], cupping_id),
            )
            conn.commit()
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            return ("该行已在出样队列中", 409)
        updated = get_cupping(cur, cupping_id)
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=updated, can_write=True)
    return redirect(url_for("queue"))


@app.get("/queue")
@login_required
def queue():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM print_queue ORDER BY id DESC")
        items = cur.fetchall()
    return render_template("queue.html", items=items, can_write=session.get("role") == "writer")


@app.post("/queue/<int:queue_id>/print")
@writer_required
def mark_printed(queue_id):
    """已打印标记只允许审评员点。"""
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """UPDATE print_queue
               SET printed = true, printed_by = %s, printed_at = now()
               WHERE id = %s AND printed = false
               RETURNING *""",
            (session["user"], queue_id),
        )
        item = cur.fetchone()
        if item is None:
            conn.rollback()
            cur.execute("SELECT * FROM print_queue WHERE id = %s", (queue_id,))
            item = cur.fetchone()
            if item is None:
                return ("队列记录不存在", 404)
        else:
            conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_queue_row.html", item=item, can_write=True)
    return redirect(url_for("queue"))

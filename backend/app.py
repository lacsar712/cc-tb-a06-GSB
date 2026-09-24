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
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "writer":
            return ("仅审评员可执行该操作", 403)
        return fn(*args, **kwargs)

    return wrap


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
        cur.execute(
            """SELECT c.*, q.id AS queue_id, q.queue_no
               FROM cuppings c
               LEFT JOIN print_queue q ON q.cupping_id = c.id
               ORDER BY c.id DESC"""
        )
        rows = cur.fetchall()
    return render_template("home.html", rows=rows, can_write=session.get("role") == "writer")


@app.post("/cuppings")
@writer_required
def create():
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"]),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row, can_write=True)
    return redirect(url_for("home"))


@app.post("/cuppings/<int:cupping_id>/edit")
@writer_required
def edit(cupping_id):
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """UPDATE cuppings
                  SET aroma=%s, taste=%s, liquor=%s, score=%s, verdict=%s, note=%s
                WHERE id=%s""",
            (aroma, taste, liquor, score, verdict, note, cupping_id),
        )
        if cur.rowcount == 0:
            return ("记录不存在", 404)
        conn.commit()
    return redirect(url_for("home"))


@app.post("/cuppings/<int:cupping_id>/enqueue")
@writer_required
def enqueue(cupping_id):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings WHERE id=%s", (cupping_id,))
        row = cur.fetchone()
        if not row:
            return ("记录不存在", 404)
        if row["verdict"] != "通过":
            # 未通过行禁止入队
            return ("未通过的审评记录禁止入队", 403)
        cur.execute("SELECT id FROM print_queue WHERE cupping_id=%s", (cupping_id,))
        if cur.fetchone():
            return ("该批次已在出样队列中", 409)
        # 入队即冻结：批次、三项分、加权分、结论原文全部复制为快照
        cur.execute(
            """INSERT INTO print_queue
                   (cupping_id, lot, aroma, taste, liquor, score, verdict, note, enqueued_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id, queue_no""",
            (
                cupping_id,
                row["lot"],
                row["aroma"],
                row["taste"],
                row["liquor"],
                row["score"],
                row["verdict"],
                row["note"],
                session["user"],
            ),
        )
        item = cur.fetchone()
        conn.commit()
    return redirect(url_for("queue_detail", queue_id=item["id"]))


@app.get("/queue")
@login_required
def queue_list():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM print_queue ORDER BY queue_no DESC")
        items = cur.fetchall()
    return render_template("queue.html", items=items, can_write=session.get("role") == "writer")


@app.get("/queue/<int:queue_id>")
@login_required
def queue_detail(queue_id):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM print_queue WHERE id=%s", (queue_id,))
        item = cur.fetchone()
    if not item:
        return ("队列正文不存在", 404)
    return render_template("queue_detail.html", q=item, can_write=session.get("role") == "writer")


@app.post("/queue/<int:queue_id>/printed")
@writer_required
def mark_printed(queue_id):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("UPDATE print_queue SET printed=TRUE WHERE id=%s", (queue_id,))
        if cur.rowcount == 0:
            return ("队列正文不存在", 404)
        conn.commit()
    return redirect(url_for("queue_detail", queue_id=queue_id))

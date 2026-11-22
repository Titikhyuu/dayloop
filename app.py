import os
from datetime import date, datetime, timedelta
from functools import wraps

from bson import ObjectId
from bson.errors import InvalidId
from flask import (Flask, jsonify, redirect, render_template, request, send_from_directory,
                   session)
from pymongo import MongoClient
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
# Change this to any long random text before sharing/deploying the project
app.secret_key = os.environ.get("SECRET_KEY", "dayloop-dev-secret-change-me")

client = MongoClient("mongodb://localhost:27017")
db = client["dayloop"]
users = db["users"]
routines = db["routines"]
logs = db["logs"]
users.create_index("email", unique=True)

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ---------- helpers ----------
def serialize(doc):
    doc["_id"] = str(doc["_id"])
    if "created_at" in doc:
        doc["created_at"] = doc["created_at"].isoformat()
    return doc


def is_due(routine, day):
    repeat = routine.get("repeat", "daily")
    return repeat == "daily" or DAYS[day.weekday()] in repeat


def days_of(repeat):
    """Turn a repeat rule ("daily" or ["mon", "wed"]) into a set of day names."""
    if repeat == "daily":
        return set(DAYS)
    return {d for d in repeat if d in DAYS}


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "login required"}), 401
        return f(*args, **kwargs)
    return wrapper


def get_owned(routine_id):
    """Return the routine only if it belongs to the logged-in user."""
    try:
        oid = ObjectId(routine_id)
    except InvalidId:
        return None
    return routines.find_one({"_id": oid, "user_id": session["user_id"]})


# ---------- pages ----------
@app.route("/")
def home():
    if "user_id" not in session:
        return redirect("/login")
    return render_template("index.html")


@app.route("/login")
def login_page():
    if "user_id" in session:
        return redirect("/")
    return render_template("auth.html")


# ---------- PWA files (must be served from the site root) ----------
@app.route("/service-worker.js")
def service_worker():
    resp = send_from_directory("static", "service-worker.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory("static", "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/offline")
def offline():
    return render_template("offline.html")


# ---------- auth API ----------
@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    if not name or "@" not in email or len(password) < 6:
        return jsonify({"error": "Enter your name, a valid email and a password of 6+ characters."}), 400
    if users.find_one({"email": email}):
        return jsonify({"error": "An account with this email already exists."}), 409
    result = users.insert_one({
        "name": name,
        "email": email,
        "password_hash": generate_password_hash(password),
        "created_at": datetime.now(),
    })
    session["user_id"] = str(result.inserted_id)
    session["name"] = name
    return jsonify({"ok": True}), 201


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    user = users.find_one({"email": email})
    if not user or not check_password_hash(user["password_hash"], data.get("password", "")):
        return jsonify({"error": "Wrong email or password."}), 401
    session["user_id"] = str(user["_id"])
    session["name"] = user["name"]
    return jsonify({"ok": True})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
@login_required
def me():
    return jsonify({"name": session["name"]})


# ---------- routines API (always scoped to the logged-in user) ----------
def read_routine_input(data):
    """Validate and clean the routine fields sent by the page. Returns (fields, error)."""
    title = (data.get("title") or "").strip()
    if not title:
        return None, "Title is required."
    repeat = data.get("repeat", "daily")  # "daily" or ["mon", "wed"]
    if repeat != "daily":
        chosen = set(repeat) if isinstance(repeat, list) else set()
        repeat = [d for d in DAYS if d in chosen]  # keep valid day names, in week order
        if not repeat:
            return None, "Pick at least one valid day."
    fields = {
        "title": title,
        "category": data.get("category", "general"),
        "time": data.get("time") or "08:00",
        "repeat": repeat,
    }
    return fields, None


def find_clash(fields, exclude_id=None):
    """Return an error message if another routine is at the same time on a shared day."""
    new_days = days_of(fields["repeat"])
    query = {"user_id": session["user_id"], "active": True, "time": fields["time"]}
    if exclude_id is not None:
        query["_id"] = {"$ne": exclude_id}  # when editing, ignore the routine itself
    for other in routines.find(query):
        shared = [d for d in DAYS if d in new_days and d in days_of(other.get("repeat", "daily"))]
        if shared:
            names = ", ".join(d.title() for d in shared)
            return (f'"{other["title"]}" is already scheduled at {fields["time"]} on {names}. '
                    "Delete it first or choose a different time or day.")
    return None


@app.route("/api/routines", methods=["POST"])
@login_required
def add_routine():
    fields, error = read_routine_input(request.get_json() or {})
    if error:
        return jsonify({"error": error}), 400
    clash = find_clash(fields)
    if clash:
        return jsonify({"error": clash}), 409
    routine = {"user_id": session["user_id"], **fields, "active": True, "created_at": datetime.now()}
    routine["_id"] = routines.insert_one(routine).inserted_id
    return jsonify(serialize(routine)), 201


@app.route("/api/routines/<routine_id>", methods=["PUT"])
@login_required
def update_routine(routine_id):
    routine = get_owned(routine_id)
    if not routine:
        return jsonify({"error": "Not found"}), 404
    fields, error = read_routine_input(request.get_json() or {})
    if error:
        return jsonify({"error": error}), 400
    clash = find_clash(fields, exclude_id=routine["_id"])
    if clash:
        return jsonify({"error": clash}), 409
    routines.update_one({"_id": routine["_id"]}, {"$set": fields})
    routine.update(fields)
    return jsonify(serialize(routine))


@app.route("/api/routines", methods=["GET"])
@login_required
def list_routines():
    docs = routines.find({"user_id": session["user_id"], "active": True}).sort("time", 1)
    return jsonify([serialize(d) for d in docs])


@app.route("/api/routines/today", methods=["GET"])
@login_required
def today_routines():
    uid = session["user_id"]
    today = date.today()
    done_ids = {l["routine_id"] for l in logs.find(
        {"user_id": uid, "date": today.isoformat(), "status": "done"})}
    items = []
    for doc in routines.find({"user_id": uid, "active": True}).sort("time", 1):
        if is_due(doc, today):
            doc = serialize(doc)
            doc["done"] = doc["_id"] in done_ids
            items.append(doc)
    return jsonify(items)


@app.route("/api/routines/<routine_id>/complete", methods=["POST"])
@login_required
def complete_routine(routine_id):
    if not get_owned(routine_id):
        return jsonify({"error": "Not found"}), 404
    today = date.today().isoformat()
    logs.update_one(
        {"user_id": session["user_id"], "routine_id": routine_id, "date": today},
        {"$set": {"status": "done", "completed_at": datetime.now()}},
        upsert=True,
    )
    return jsonify({"status": "done"})


@app.route("/api/routines/<routine_id>/undo", methods=["POST"])
@login_required
def undo_routine(routine_id):
    if not get_owned(routine_id):
        return jsonify({"error": "Not found"}), 404
    logs.delete_one({"user_id": session["user_id"], "routine_id": routine_id,
                     "date": date.today().isoformat()})
    return jsonify({"status": "pending"})


@app.route("/api/routines/<routine_id>", methods=["DELETE"])
@login_required
def delete_routine(routine_id):
    routine = get_owned(routine_id)
    if not routine:
        return jsonify({"error": "Not found"}), 404
    routines.delete_one({"_id": routine["_id"]})
    logs.delete_many({"user_id": session["user_id"], "routine_id": routine_id})
    return jsonify({"deleted": routine_id})


# ---------- progress ----------
# ---------- progress helpers (shared by /api/stats and /api/summary) ----------
def load_history(uid):
    """The user's active routines, and a map of date -> set of routine ids completed that day."""
    mine = list(routines.find({"user_id": uid, "active": True}))
    done_by_day = {}
    for l in logs.find({"user_id": uid, "status": "done"}):
        done_by_day.setdefault(l["date"], set()).add(l["routine_id"])
    return mine, done_by_day


def day_counts(mine, done_by_day, d, today, now_hhmm):
    """Routines scheduled on day d, how many were done, and how many are still to come today.
    A routine counts from the day it was created. Today only counts a routine once its time
    has arrived (or it is done), so the morning is not treated as 'missed'."""
    due = [r for r in mine if r["created_at"].date() <= d and is_due(r, d)]
    done_ids = done_by_day.get(d.isoformat(), set())
    upcoming = 0
    if d == today:
        kept = [r for r in due if str(r["_id"]) in done_ids or r["time"] <= now_hhmm]
        upcoming = len(due) - len(kept)
        due = kept
    done = sum(1 for r in due if str(r["_id"]) in done_ids)
    return due, done, upcoming, done_ids


def rate_of(chunk):
    total_due = sum(x["due"] for x in chunk)
    total_done = sum(x["done"] for x in chunk)
    return round(total_done / total_due * 100) if total_due else None


@app.route("/api/stats")
@login_required
def stats():
    uid = session["user_id"]
    today = date.today()
    now_hhmm = datetime.now().strftime("%H:%M")
    mine, done_by_day = load_history(uid)

    # last 28 days, oldest first
    days, due_ids = [], []
    for i in range(27, -1, -1):
        d = today - timedelta(days=i)
        due, done, upcoming, _ = day_counts(mine, done_by_day, d, today, now_hhmm)
        days.append({"date": d.isoformat(), "weekday": d.weekday(), "label": DAYS[d.weekday()].title(),
                     "due": len(due), "done": done, "upcoming": upcoming})
        due_ids.append([str(r["_id"]) for r in due])

    # per routine over the same 28 days
    per = {str(r["_id"]): {"title": r["title"], "category": r.get("category", "general"),
                           "time": r["time"], "due": 0, "done": 0} for r in mine}
    for day, ids in zip(days, due_ids):
        done_ids = done_by_day.get(day["date"], set())
        for rid in ids:
            per[rid]["due"] += 1
            if rid in done_ids:
                per[rid]["done"] += 1
    routine_stats = []
    for item in per.values():
        if item["due"]:
            item["rate"] = round(item["done"] / item["due"] * 100)
            routine_stats.append(item)

    # per weekday over the same 28 days
    weekday = [{"label": DAYS[i].title(), "due": 0, "done": 0} for i in range(7)]
    for day in days:
        weekday[day["weekday"]]["due"] += day["due"]
        weekday[day["weekday"]]["done"] += day["done"]

    # current streak: consecutive days (ending today) with at least one routine completed
    streak, d = 0, today
    if not done_by_day.get(d.isoformat()):
        d -= timedelta(days=1)  # today not started yet: do not break the streak
    while done_by_day.get(d.isoformat()):
        streak += 1
        d -= timedelta(days=1)

    # longest streak ever
    longest, run, prev = 0, 0, None
    for d in sorted(date.fromisoformat(k) for k, v in done_by_day.items() if v):
        run = run + 1 if prev is not None and (d - prev).days == 1 else 1
        longest = max(longest, run)
        prev = d

    last7 = days[-7:]
    return jsonify({
        "streak": streak,
        "longest_streak": longest,
        "rate": rate_of(last7) or 0,            # last 7 days
        "prev_rate": rate_of(days[-14:-7]),     # the 7 days before that (None if no data)
        "month_rate": rate_of(days),            # last 28 days (None if no data)
        "week": [{"label": x["label"], "due": x["due"], "done": x["done"]} for x in last7],
        "days": days,
        "weekday": weekday,
        "routines": routine_stats,
    })


# ---------- weekly / monthly / yearly summaries ----------
def period_bounds(period, offset, today):
    """First and last date of the calendar week (Mon-Sun), month or year that is `offset` periods
    from the current one (0 = current, -1 = previous, ...)."""
    if period == "week":
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        return start, start + timedelta(days=6)
    if period == "month":
        total = today.year * 12 + (today.month - 1) + offset
        y, m0 = divmod(total, 12)
        ny, nm0 = divmod(total + 1, 12)
        return date(y, m0 + 1, 1), date(ny, nm0 + 1, 1) - timedelta(days=1)
    y = today.year + offset
    return date(y, 1, 1), date(y, 12, 31)


def summarize_range(mine, done_by_day, start, end, today, now_hhmm):
    """Totals, per-day figures, per-routine and per-weekday results for start..end (days after today are future)."""
    per = {str(r["_id"]): {"title": r["title"], "category": r.get("category", "general"),
                           "time": r["time"], "due": 0, "done": 0} for r in mine}
    weekday = [{"label": DAYS[i].title(), "due": 0, "done": 0} for i in range(7)]
    days = []
    d = start
    while d <= end:
        item = {"date": d.isoformat(), "weekday": d.weekday(), "label": DAYS[d.weekday()].title(),
                "due": 0, "done": 0, "upcoming": 0, "future": d > today}
        if d <= today:
            due, done, upcoming, done_ids = day_counts(mine, done_by_day, d, today, now_hhmm)
            item.update(due=len(due), done=done, upcoming=upcoming)
            for r in due:
                rid = str(r["_id"])
                per[rid]["due"] += 1
                if rid in done_ids:
                    per[rid]["done"] += 1
            weekday[d.weekday()]["due"] += len(due)
            weekday[d.weekday()]["done"] += done
        days.append(item)
        d += timedelta(days=1)

    counted = [x for x in days if not x["future"]]
    today_iso = today.isoformat()
    longest = run = 0
    for x in counted:  # longest run of days with at least one completion inside this period
        run = run + 1 if done_by_day.get(x["date"]) else 0
        longest = max(longest, run)
    routine_stats = []
    for item in per.values():
        if item["due"]:
            item["rate"] = round(item["done"] / item["due"] * 100)
            routine_stats.append(item)
    return {
        "days": days,
        "due": sum(x["due"] for x in counted),
        "done": sum(x["done"] for x in counted),
        "rate": rate_of(counted),
        "perfect_days": sum(1 for x in counted if x["due"] and x["done"] == x["due"] and not x["upcoming"]),
        "missed_days": sum(1 for x in counted if x["due"] and x["done"] == 0 and x["date"] < today_iso),
        "active_days": sum(1 for x in counted if done_by_day.get(x["date"])),
        "scheduled_days": sum(1 for x in counted if x["due"]),
        "longest_streak": longest,
        "weekday": weekday,
        "routines": routine_stats,
    }


@app.route("/api/summary")
@login_required
def summary():
    period = request.args.get("period", "week")
    if period not in ("week", "month", "year"):
        return jsonify({"error": "Period must be week, month or year."}), 400
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0
    offset = max(min(offset, 0), {"week": -520, "month": -240, "year": -20}[period])  # never into the future

    today = date.today()
    now_hhmm = datetime.now().strftime("%H:%M")
    mine, done_by_day = load_history(session["user_id"])

    start, end = period_bounds(period, offset, today)
    prev_start, prev_end = period_bounds(period, offset - 1, today)
    current = summarize_range(mine, done_by_day, start, end, today, now_hhmm)
    previous = summarize_range(mine, done_by_day, prev_start, prev_end, today, now_hhmm)
    earliest = min((r["created_at"].date() for r in mine), default=None)

    result = {"period": period, "offset": offset, "today": today.isoformat(),
              "start": start.isoformat(), "end": end.isoformat(), "in_progress": end >= today,
              "has_prev": earliest is not None and earliest < start,
              "prev_rate": previous["rate"], **current}
    if period == "year":
        months = []
        for m in range(1, 13):
            chunk = [x for x in current["days"] if int(x["date"][5:7]) == m]
            counted = [x for x in chunk if not x["future"]]
            months.append({"month": m, "label": date(start.year, m, 1).strftime("%b"),
                           "due": sum(x["due"] for x in counted), "done": sum(x["done"] for x in counted),
                           "future": not counted})
        result["months"] = months
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True)
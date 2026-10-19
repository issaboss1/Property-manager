import os
import secrets

from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from flask import (Flask, Response, abort, flash, g, redirect, render_template,  # noqa: E402
                   request, send_file, url_for)

import db  # noqa: E402
from notify import send_email, send_whatsapp_template, whatsapp_configured  # noqa: E402
from receipts import build_receipt, money, period_label, receipt_path  # noqa: E402
from reminders import (TZ, collect_alerts, due_date, next_service_due, paid_for_period,  # noqa: E402
                       period_of, rent_applies, run_reminders, today)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
app.jinja_env.filters["money"] = money
app.jinja_env.filters["period"] = period_label


def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@app.before_request
def require_password():
    # Optional: set APP_PASSWORD in .env to protect the dashboard (needed once it's online).
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return None
    auth = request.authorization
    if auth and secrets.compare_digest(auth.password or "", password):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Property Manager"'})


@app.context_processor
def inject_globals():
    return {"business_name": os.environ.get("BUSINESS_NAME", "Property Manager"), "today": today(),
            "whatsapp_on": whatsapp_configured()}


def num(value, cast=float, default=0):
    try:
        return cast(value) if value not in (None, "") else default
    except ValueError:
        return default


# ---------- Dashboard ----------

@app.route("/")
def dashboard():
    conn = get_db()
    on = today()
    period = period_of(on)
    rent_rows = []
    for t in conn.execute("""SELECT t.*, u.name AS unit_name FROM tenants t LEFT JOIN units u ON u.id = t.unit_id
                             WHERE t.active = 1 ORDER BY u.name, t.name"""):
        due = due_date(period, t["rent_due_day"])
        if not rent_applies(t, due):
            continue
        paid = paid_for_period(conn, t["id"], period)
        balance = t["rent_amount"] - paid
        status = "Paid" if balance <= 0 else ("Late" if on > due else ("Partial" if paid else "Due"))
        rent_rows.append(dict(t=t, due=due, paid=paid, balance=balance, status=status))

    recent = conn.execute("""SELECT p.*, t.name AS tenant_name FROM payments p JOIN tenants t ON t.id = p.tenant_id
                             ORDER BY p.paid_on DESC, p.id DESC LIMIT 8""").fetchall()
    collected = sum(r["paid"] for r in rent_rows)
    expected = sum(r["t"]["rent_amount"] for r in rent_rows)
    return render_template("dashboard.html", alerts=collect_alerts(conn, on), rent_rows=rent_rows,
                           recent=recent, period=period, collected=collected, expected=expected)


@app.post("/reminders/run")
def reminders_run():
    flash(run_reminders(get_db()))
    return redirect(url_for("dashboard"))


# ---------- Units ----------

@app.route("/units")
def units():
    rows = get_db().execute("""SELECT u.*, (SELECT name FROM tenants WHERE unit_id = u.id AND active = 1 LIMIT 1)
                               AS tenant_name FROM units u ORDER BY name""").fetchall()
    return render_template("units.html", units=rows)


@app.route("/units/new", methods=["GET", "POST"])
@app.route("/units/<int:unit_id>/edit", methods=["GET", "POST"])
def unit_form(unit_id=None):
    conn = get_db()
    unit = conn.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone() if unit_id else None
    if unit_id and not unit:
        abort(404)
    if request.method == "POST":
        f = request.form
        values = (f["name"].strip(), f.get("address", "").strip(), f.get("notes", "").strip())
        if unit:
            conn.execute("UPDATE units SET name = ?, address = ?, notes = ? WHERE id = ?", (*values, unit_id))
        else:
            conn.execute("INSERT INTO units (name, address, notes) VALUES (?, ?, ?)", values)
        conn.commit()
        flash("Unit saved.")
        return redirect(url_for("units"))
    return render_template("unit_form.html", unit=unit)


# ---------- Tenants ----------

@app.route("/tenants")
def tenants():
    rows = get_db().execute("""SELECT t.*, u.name AS unit_name FROM tenants t LEFT JOIN units u ON u.id = t.unit_id
                               ORDER BY t.active DESC, u.name, t.name""").fetchall()
    return render_template("tenants.html", tenants=rows)


@app.route("/tenants/new", methods=["GET", "POST"])
@app.route("/tenants/<int:tenant_id>/edit", methods=["GET", "POST"])
def tenant_form(tenant_id=None):
    conn = get_db()
    tenant = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone() if tenant_id else None
    if tenant_id and not tenant:
        abort(404)
    if request.method == "POST":
        f = request.form
        values = (
            f["name"].strip(), f.get("email", "").strip(), f.get("whatsapp", "").strip(),
            num(f.get("unit_id"), int, None), f.get("lease_start") or None, f.get("lease_end") or None,
            num(f.get("rent_amount")), min(max(num(f.get("rent_due_day"), int, 1), 1), 28), num(f.get("deposit")),
            1 if f.get("send_email") else 0, 1 if f.get("send_whatsapp") else 0, 1 if f.get("active") else 0,
            f.get("notes", "").strip(),
        )
        cols = ("name, email, whatsapp, unit_id, lease_start, lease_end, rent_amount, rent_due_day, deposit, "
                "send_email, send_whatsapp, active, notes")
        if tenant:
            sets = ", ".join(f"{c.strip()} = ?" for c in cols.split(","))
            conn.execute(f"UPDATE tenants SET {sets} WHERE id = ?", (*values, tenant_id))
        else:
            cur = conn.execute(f"INSERT INTO tenants ({cols}) VALUES ({', '.join('?' * len(values))})", values)
            tenant_id = cur.lastrowid
        conn.commit()
        flash("Tenant saved.")
        return redirect(url_for("tenant_detail", tenant_id=tenant_id))
    units_list = conn.execute("SELECT * FROM units ORDER BY name").fetchall()
    return render_template("tenant_form.html", tenant=tenant, units=units_list)


@app.route("/tenants/<int:tenant_id>")
def tenant_detail(tenant_id):
    conn = get_db()
    tenant = conn.execute("""SELECT t.*, u.name AS unit_name FROM tenants t LEFT JOIN units u ON u.id = t.unit_id
                             WHERE t.id = ?""", (tenant_id,)).fetchone()
    if not tenant:
        abort(404)
    payments = conn.execute("SELECT * FROM payments WHERE tenant_id = ? ORDER BY paid_on DESC, id DESC",
                            (tenant_id,)).fetchall()
    return render_template("tenant_detail.html", tenant=tenant, payments=payments,
                           total=sum(p["amount"] for p in payments))


# ---------- Payments & receipts ----------

def send_receipt(conn, payment_id, via_email=True, via_whatsapp=True):
    """Build the PDF receipt and send it to the tenant. Returns messages describing what happened."""
    p = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    t = conn.execute("""SELECT t.*, u.name AS unit_name FROM tenants t LEFT JOIN units u ON u.id = t.unit_id
                        WHERE t.id = ?""", (p["tenant_id"],)).fetchone()
    balance = t["rent_amount"] - paid_for_period(conn, t["id"], p["period"])
    path = build_receipt(p, t, t["unit_name"], balance)
    business = os.environ.get("BUSINESS_NAME", "Property Management")

    messages = []
    if via_email:
        body = (f"Hi {t['name']},\n\nThank you for your payment of {money(p['amount'])} for "
                f"{period_label(p['period'])}. Your receipt ({p['receipt_no']}) is attached.\n\n"
                f"Regards,\n{business}")
        ok, info = send_email(t["email"], f"Receipt {p['receipt_no']} - {business}", body, path)
        messages.append(f"Email: {info}")
    if via_whatsapp and whatsapp_configured():
        ok, info = send_whatsapp_template(t["whatsapp"], os.environ.get("WA_TEMPLATE_RECEIPT", "payment_receipt"),
                                          [t["name"], money(p["amount"]), p["receipt_no"]], document=path)
        messages.append(f"WhatsApp: {info}")
    return messages


@app.route("/payments/new", methods=["GET", "POST"])
def payment_form():
    conn = get_db()
    if request.method == "POST":
        f = request.form
        tenant_id = num(f.get("tenant_id"), int, None)
        amount = num(f.get("amount"))
        if not tenant_id or amount <= 0:
            flash("Choose a tenant and enter an amount.")
            return redirect(url_for("payment_form"))
        cur = conn.execute(
            "INSERT INTO payments (tenant_id, amount, method, reference, paid_on, period, notes) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, amount, f.get("method", "cash"), f.get("reference", "").strip(),
             f.get("paid_on") or today().isoformat(), f.get("period") or period_of(today()), f.get("notes", "").strip()))
        pid = cur.lastrowid
        conn.execute("UPDATE payments SET receipt_no = ? WHERE id = ?", (f"R{(f.get('paid_on') or str(today()))[:4]}-{pid:04d}", pid))
        conn.commit()
        results = send_receipt(conn, pid, bool(f.get("send_email")), bool(f.get("send_whatsapp")))
        flash(f"Payment of {money(amount)} recorded. Receipt created. " + " | ".join(results))
        return redirect(url_for("tenant_detail", tenant_id=tenant_id))

    tenants_list = conn.execute("""SELECT t.*, u.name AS unit_name FROM tenants t LEFT JOIN units u ON u.id = t.unit_id
                                   WHERE t.active = 1 ORDER BY u.name, t.name""").fetchall()
    selected = num(request.args.get("tenant_id"), int, None)
    return render_template("payment_form.html", tenants=tenants_list, selected=selected, period=period_of(today()))


@app.route("/payments/<int:payment_id>/receipt")
def payment_receipt(payment_id):
    conn = get_db()
    p = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not p:
        abort(404)
    path = receipt_path(p["receipt_no"])
    if not os.path.exists(path):
        send_receipt(conn, payment_id, via_email=False, via_whatsapp=False)
    return send_file(path, mimetype="application/pdf", download_name=f"{p['receipt_no']}.pdf")


@app.post("/payments/<int:payment_id>/resend")
def payment_resend(payment_id):
    conn = get_db()
    p = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not p:
        abort(404)
    t = conn.execute("SELECT * FROM tenants WHERE id = ?", (p["tenant_id"],)).fetchone()
    flash(f"Receipt {p['receipt_no']}: " + " | ".join(
        send_receipt(conn, payment_id, bool(t["send_email"]), bool(t["send_whatsapp"]))))
    return redirect(url_for("tenant_detail", tenant_id=p["tenant_id"]))


@app.post("/payments/<int:payment_id>/delete")
def payment_delete(payment_id):
    conn = get_db()
    p = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not p:
        abort(404)
    conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
    conn.commit()
    flash(f"Payment {p['receipt_no']} deleted.")
    return redirect(url_for("tenant_detail", tenant_id=p["tenant_id"]))


# ---------- Servicing ----------

@app.route("/service")
def service():
    conn = get_db()
    on = today()
    items = []
    for s in conn.execute("""SELECT s.*, u.name AS unit_name FROM service_items s
                             LEFT JOIN units u ON u.id = s.unit_id ORDER BY u.name, s.name"""):
        nxt = next_service_due(s, on)
        items.append(dict(s=s, next_due=nxt, days=(nxt - on).days))
    items.sort(key=lambda i: i["next_due"])
    log = conn.execute("""SELECT l.*, s.name AS item_name, u.name AS unit_name FROM service_log l
                          JOIN service_items s ON s.id = l.item_id LEFT JOIN units u ON u.id = s.unit_id
                          ORDER BY l.serviced_on DESC LIMIT 20""").fetchall()
    units_list = conn.execute("SELECT * FROM units ORDER BY name").fetchall()
    return render_template("service.html", items=items, log=log, units=units_list)


@app.post("/service/new")
def service_new():
    f = request.form
    conn = get_db()
    conn.execute("INSERT INTO service_items (unit_id, name, interval_months, last_serviced, notes) VALUES (?,?,?,?,?)",
                 (num(f.get("unit_id"), int, None), f["name"].strip(), max(num(f.get("interval_months"), int, 6), 1),
                  f.get("last_serviced") or None, f.get("notes", "").strip()))
    conn.commit()
    flash("Service item added.")
    return redirect(url_for("service"))


@app.post("/service/<int:item_id>/done")
def service_done(item_id):
    f = request.form
    conn = get_db()
    on = f.get("serviced_on") or today().isoformat()
    conn.execute("INSERT INTO service_log (item_id, serviced_on, cost, notes) VALUES (?,?,?,?)",
                 (item_id, on, num(f.get("cost"), float, None), f.get("notes", "").strip()))
    conn.execute("UPDATE service_items SET last_serviced = ? WHERE id = ?", (on, item_id))
    conn.commit()
    flash("Service recorded.")
    return redirect(url_for("service"))


@app.post("/service/<int:item_id>/delete")
def service_delete(item_id):
    conn = get_db()
    conn.execute("DELETE FROM service_items WHERE id = ?", (item_id,))
    conn.commit()
    flash("Service item removed.")
    return redirect(url_for("service"))


# ---------- Startup ----------

def daily_job():
    conn = db.connect()
    try:
        print("[reminders]", run_reminders(conn))
    finally:
        conn.close()


def start_scheduler():
    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.add_job(daily_job, "cron", hour=int(os.environ.get("REMINDER_HOUR", "8")), minute=0)
    scheduler.start()


db.init_db()

if __name__ == "__main__":
    start_scheduler()
    daily_job()  # catch up on anything missed while the app was off
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  Property Manager running at http://localhost:{port}\n")
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port, debug=False)

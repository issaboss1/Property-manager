"""Works out what needs attention (leases, rent, servicing) and sends the alerts.

Every alert has a unique key. Once an alert is sent its key is stored, so each
reminder goes out once per stage even if the app restarts or was switched off
on the exact day.
"""
import calendar
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from notify import notify_owner, send_email, send_whatsapp_template
from receipts import money, period_label

TZ = ZoneInfo("America/Port_of_Spain")


def today():
    return datetime.now(TZ).date()


def parse(s):
    return date.fromisoformat(s) if s else None


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def period_of(d):
    return d.strftime("%Y-%m")


def due_date(period, due_day):
    y, m = map(int, period.split("-"))
    return date(y, m, min(due_day, calendar.monthrange(y, m)[1]))


def paid_for_period(conn, tenant_id, period):
    return conn.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE tenant_id = ? AND period = ?",
                        (tenant_id, period)).fetchone()[0]


def rent_applies(tenant, due):
    start, end = parse(tenant["lease_start"]), parse(tenant["lease_end"])
    if period_of(due) < tenant["track_from"]:
        return False
    return not (start and due < start) and not (end and due > end)


def next_service_due(item, on):
    last = parse(item["last_serviced"])
    return add_months(last, item["interval_months"]) if last else on


def collect_alerts(conn, on=None):
    on = on or today()
    alerts = []
    tenants = conn.execute("""SELECT t.*, u.name AS unit_name FROM tenants t
                              LEFT JOIN units u ON u.id = t.unit_id WHERE t.active = 1""").fetchall()

    for t in tenants:
        who = f"{t['name']} ({t['unit_name'] or 'no unit'})"

        # Lease ending: alerts at 90, 60, 30 and 7 days, then once expired.
        end = parse(t["lease_end"])
        if end and (end - on).days <= 90:
            days = (end - on).days
            if days < 0:
                stage, level = "expired", "urgent"
                msg = f"Lease for {who} ended {-days} day(s) ago ({end:%d %b %Y}). Renew or update their record."
            else:
                stage = min(s for s in (90, 60, 30, 7) if days <= s)
                level = "urgent" if days <= 30 else "soon"
                msg = f"Lease for {who} ends in {days} day(s) on {end:%d %b %Y}. Time to renew or give notice."
            alerts.append(dict(key=f"lease:{t['id']}:{end}:{stage}", kind="Lease", level=level,
                               message=msg, tenant_id=t["id"], owner=True))

        # Rent: check last month and this month.
        for period in {period_of(add_months(on.replace(day=1), -1)), period_of(on)}:
            due = due_date(period, t["rent_due_day"])
            if not rent_applies(t, due):
                continue
            balance = t["rent_amount"] - paid_for_period(conn, t["id"], period)
            if balance <= 0:
                continue
            late = (on - due).days
            if late >= 1:
                stage = max(s for s in (1, 3, 7, 14) if late >= s)
                alerts.append(dict(
                    key=f"rent_late:{t['id']}:{period}:{stage}", kind="Rent", level="urgent", tenant_id=t["id"],
                    owner=True,
                    message=f"{who} owes {money(balance)} for {period_label(period)} ({late} day(s) late).",
                    tenant_reminder=(t, balance, due, True)))
            elif late >= -3 and period == period_of(on):
                alerts.append(dict(
                    key=f"rent_due:{t['id']}:{period}", kind="Rent", level="info", tenant_id=t["id"], owner=False,
                    message=f"{who}: rent of {money(balance)} due {due:%d %b}.",
                    tenant_reminder=(t, balance, due, False)))

    # Servicing (AC units, fire extinguishers, water tanks, ...): alerts at 14 and 7 days, on the day, overdue.
    items = conn.execute("""SELECT s.*, u.name AS unit_name FROM service_items s
                            LEFT JOIN units u ON u.id = s.unit_id""").fetchall()
    for s in items:
        nxt = next_service_due(s, on)
        days = (nxt - on).days
        if days > 14:
            continue
        what = f"{s['name']} at {s['unit_name']}" if s["unit_name"] else s["name"]
        if days < 0:
            stage, level, msg = "overdue", "urgent", f"{what} is overdue for service (was due {nxt:%d %b %Y})."
        else:
            stage = min(x for x in (14, 7, 0) if days <= x)
            level = "urgent" if days <= 7 else "soon"
            msg = f"{what} needs servicing " + ("today." if days == 0 else f"in {days} day(s) ({nxt:%d %b}).")
        alerts.append(dict(key=f"service:{s['id']}:{nxt}:{stage}", kind="Service", level=level,
                           message=msg, owner=True))

    order = {"urgent": 0, "soon": 1, "info": 2}
    alerts.sort(key=lambda a: order[a["level"]])
    return alerts


def remind_tenant(tenant, balance, due, late):
    """Polite rent reminder to the tenant by their preferred channels."""
    results = []
    amount, when = money(balance), due.strftime("%d %B %Y")
    business = os.environ.get("BUSINESS_NAME", "Property Management")
    if late:
        subject = f"Rent reminder: {amount} outstanding"
        body = (f"Hi {tenant['name']},\n\nThis is a friendly reminder that rent of {amount} was due on {when} "
                f"and has not been received yet. If you've already paid, please ignore this message.\n\n"
                f"Thank you,\n{business}")
    else:
        subject = f"Rent of {amount} due {when}"
        body = (f"Hi {tenant['name']},\n\nJust a reminder that your rent of {amount} is due on {when}.\n\n"
                f"Thank you,\n{business}")
    if tenant["send_email"] and tenant["email"]:
        results.append(send_email(tenant["email"], subject, body)[0])
    if tenant["send_whatsapp"] and tenant["whatsapp"]:
        results.append(send_whatsapp_template(tenant["whatsapp"], os.environ.get("WA_TEMPLATE_RENT_REMINDER", "rent_reminder"),
                                              [tenant["name"], amount, when])[0])
    return any(results)


def run_reminders(conn, on=None):
    """Send any alerts that haven't gone out yet. Returns a short summary."""
    sent = {r["key"] for r in conn.execute("SELECT key FROM sent_alerts")}
    new = [a for a in collect_alerts(conn, on) if a["key"] not in sent]
    if not new:
        return "Nothing new to send."

    tenant_reminders = os.environ.get("SEND_TENANT_REMINDERS", "true").lower() == "true"
    delivered_keys = []

    owner_alerts = [a for a in new if a["owner"]]
    if owner_alerts and notify_owner(f"Property alerts ({len(owner_alerts)})", [a["message"] for a in owner_alerts]):
        delivered_keys += [a["key"] for a in owner_alerts]

    tenants_reminded = 0
    for a in new:
        if tenant_reminders and a.get("tenant_reminder"):
            if remind_tenant(*a["tenant_reminder"]):
                tenants_reminded += 1
                if a["key"] not in delivered_keys:
                    delivered_keys.append(a["key"])

    conn.executemany("INSERT OR IGNORE INTO sent_alerts (key) VALUES (?)", [(k,) for k in delivered_keys])
    conn.commit()

    if not delivered_keys:
        return (f"{len(new)} alert(s) found but nothing was delivered. Set up email or WhatsApp in .env "
                f"(details were printed in the app window).")
    return f"Sent {len(owner_alerts)} alert(s) to you and {tenants_reminded} reminder(s) to tenants."

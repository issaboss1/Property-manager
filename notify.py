"""Sending email and WhatsApp messages.

Both channels are optional: if one isn't configured in .env, the message is
printed to the console instead and reported as "not configured".
"""
import mimetypes
import os
import re
import smtplib
from email.message import EmailMessage

import requests


def _env(key, default=""):
    return os.environ.get(key, default).strip()


# ---------- Email (any SMTP server; Gmail works with an App Password) ----------

def email_configured():
    return bool(_env("SMTP_USER") and _env("SMTP_PASSWORD"))


def send_email(to, subject, body, attachment=None):
    if not to:
        return False, "no email address"
    if not email_configured():
        print(f"[email not configured] To: {to} | {subject}\n{body}\n")
        return False, "email not configured"

    msg = EmailMessage()
    msg["From"] = _env("SMTP_FROM") or _env("SMTP_USER")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment:
        ctype = mimetypes.guess_type(attachment)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with open(attachment, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(attachment))
    try:
        with smtplib.SMTP(_env("SMTP_HOST", "smtp.gmail.com"), int(_env("SMTP_PORT", "587")), timeout=30) as s:
            s.starttls()
            s.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
            s.send_message(msg)
        return True, "sent"
    except Exception as e:  # noqa: BLE001 - report any SMTP failure back to the UI
        return False, str(e)


# ---------- WhatsApp (Meta WhatsApp Cloud API) ----------

def normalize_phone(phone):
    """Turn '868-555-1234', '555 1234' or '+1 (868) 555-1234' into '18685551234'."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 7:
        digits = "1868" + digits
    elif len(digits) == 10 and digits.startswith("868"):
        digits = "1" + digits
    return digits


def whatsapp_configured():
    return bool(_env("WA_TOKEN") and _env("WA_PHONE_NUMBER_ID"))


def _wa_url(path):
    return f"https://graph.facebook.com/{_env('WA_API_VERSION', 'v21.0')}/{_env('WA_PHONE_NUMBER_ID')}/{path}"


def _wa_headers():
    return {"Authorization": f"Bearer {_env('WA_TOKEN')}"}


def _clean_param(text):
    # WhatsApp template parameters can't contain newlines or long runs of spaces.
    text = re.sub(r"\s*\n\s*", " | ", str(text))
    text = re.sub(r"\s{2,}", " ", text)
    return text[:1000]


def _upload_media(path):
    with open(path, "rb") as f:
        r = requests.post(
            _wa_url("media"), headers=_wa_headers(), timeout=60,
            data={"messaging_product": "whatsapp", "type": "application/pdf"},
            files={"file": (os.path.basename(path), f, "application/pdf")},
        )
    r.raise_for_status()
    return r.json()["id"]


def send_whatsapp_template(to, template, body_params, document=None):
    """Send an approved WhatsApp template, optionally with a PDF in the header."""
    to = normalize_phone(to)
    if not to:
        return False, "no WhatsApp number"
    if not whatsapp_configured():
        print(f"[whatsapp not configured] To: {to} | template {template}: {body_params}\n")
        return False, "WhatsApp not configured"

    components = []
    try:
        if document:
            media_id = _upload_media(document)
            components.append({"type": "header", "parameters": [{
                "type": "document",
                "document": {"id": media_id, "filename": os.path.basename(document)},
            }]})
        if body_params:
            components.append({"type": "body", "parameters": [
                {"type": "text", "text": _clean_param(p)} for p in body_params
            ]})
        r = requests.post(_wa_url("messages"), headers=_wa_headers(), timeout=30, json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": _env("WA_TEMPLATE_LANG", "en")},
                "components": components,
            },
        })
        if r.ok:
            return True, "sent"
        try:
            return False, r.json().get("error", {}).get("message", r.text)
        except ValueError:
            return False, r.text
    except requests.RequestException as e:
        return False, str(e)


# ---------- Owner alerts ----------

def notify_owner(subject, lines):
    """Send the owner a summary by email and/or WhatsApp. Returns True if anything got through."""
    body = "\n".join(f"- {line}" for line in lines)
    delivered = False
    if not (_env("OWNER_EMAIL") or _env("OWNER_WHATSAPP")):
        print(f"[owner alerts - set OWNER_EMAIL / OWNER_WHATSAPP in .env to receive these]\n{body}\n")
    if _env("OWNER_EMAIL"):
        ok, _ = send_email(_env("OWNER_EMAIL"), subject, body + "\n\nOpen your dashboard for details.")
        delivered = delivered or ok
    if _env("OWNER_WHATSAPP"):
        ok, _ = send_whatsapp_template(_env("OWNER_WHATSAPP"), _env("WA_TEMPLATE_OWNER_ALERT", "owner_alert"),
                                       ["; ".join(lines)])
        delivered = delivered or ok
    return delivered

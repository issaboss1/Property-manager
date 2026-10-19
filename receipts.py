import os
from datetime import date

from fpdf import FPDF

from db import DB_PATH

RECEIPTS_DIR = os.path.join(os.path.dirname(DB_PATH), "receipts")
# Optional picture of your signature, printed above the signature line on every receipt.
SIGNATURE_IMAGE = os.environ.get("SIGNATURE_IMAGE") or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                     "signature.png")


def money(amount):
    return f"TT${amount:,.2f}"


def period_label(period):
    y, m = map(int, period.split("-"))
    return date(y, m, 1).strftime("%B %Y")


def receipt_path(receipt_no):
    return os.path.join(RECEIPTS_DIR, f"{receipt_no}.pdf")


def build_receipt(payment, tenant, unit_name, balance):
    """Create the PDF receipt for a payment and return its file path."""
    os.makedirs(RECEIPTS_DIR, exist_ok=True)
    business = os.environ.get("BUSINESS_NAME", "Property Management")
    contact = [os.environ.get("BUSINESS_ADDRESS", ""), os.environ.get("BUSINESS_PHONE", ""),
               os.environ.get("OWNER_EMAIL", "")]

    pdf = FPDF(format=(148, 210))  # A5
    pdf.set_auto_page_break(True, margin=12)
    pdf.add_page()
    pdf.set_margins(12, 12, 12)

    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 8, business, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for line in filter(None, contact):
        pdf.cell(0, 4.5, line, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_fill_color(30, 64, 120)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 9, "  RENT RECEIPT", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    paid_on = date.fromisoformat(payment["paid_on"]).strftime("%d %B %Y")
    rows = [
        ("Receipt no.", payment["receipt_no"]),
        ("Date received", paid_on),
        ("Received from", tenant["name"]),
        ("Unit", unit_name or "-"),
        ("Rent for", period_label(payment["period"])),
        ("Payment method", "Bank transfer" if payment["method"] == "bank" else "Cash"),
    ]
    if payment["reference"]:
        rows.append(("Reference", payment["reference"]))

    for label, value in rows:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(38, 7, label)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, str(value), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(12, pdf.get_y(), pdf.w - 12, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(38, 9, "Amount paid")
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, money(payment["amount"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(38, 7, "Balance for month")
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, money(max(balance, 0)) if balance > 0 else "Paid in full", new_x="LMARGIN", new_y="NEXT")

    if payment["notes"]:
        pdf.ln(2)
        pdf.set_font("Helvetica", "I", 9)
        pdf.multi_cell(0, 5, f"Note: {payment['notes']}")

    pdf.ln(10)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, "Thank you for your payment.", new_x="LMARGIN", new_y="NEXT")
    if os.path.isfile(SIGNATURE_IMAGE):
        pdf.ln(2)
        pdf.image(SIGNATURE_IMAGE, x=14, h=14)  # sits just above the signature line
    else:
        pdf.ln(8)
    pdf.cell(55, 5, "_" * 30, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(55, 5, f"For {business}")

    path = receipt_path(payment["receipt_no"])
    pdf.output(path)
    return path

# -----------------------------------------------------------------------
# razorpay_donation.py
#
# Isko apne custom app me donation.py ke saath rakho:
#   apps/<your_app>/<your_app>/api/razorpay_donation.py
# Dotted path get_trust_list jaisa hi rahega, bas last part badlega:
#   <your_app>.api.razorpay_donation.create_razorpay_order  (etc.)
#
# Flow:
#   1) create_donation (Server Script) -> Sales Invoice (submitted, unpaid)
#   2) create_razorpay_order  -> Razorpay Order banata hai
#   3) Browser me Razorpay Checkout khulta hai, donor pay karta hai
#   4) verify_razorpay_payment -> signature verify -> Payment Entry + email
#   5) razorpay_webhook -> backup (browser band ho gaya to bhi payment record)
#
# site_config keys (bench set-config):
#   razorpay_key_id, razorpay_key_secret, razorpay_webhook_secret
#   (optional) razorpay_accounts = {"<Company>": {"key_id":..,"key_secret":..,"webhook_secret":..}}
#   -> har trust ka alag Razorpay account ho to use karo, warna default keys chalengi
# -----------------------------------------------------------------------

import hmac
import hashlib
import json

import requests
import frappe
from frappe import _
from frappe.utils import flt, nowdate

ORDERS_URL = "https://api.razorpay.com/v1/orders"


def get_razorpay_credentials(company):
    # ---- Primary source: Company doctype ke apne fields ----
    # (Company form kholo -> "Razorpay (Donation Gateway)" section)
    key_id = frappe.db.get_value("Company", company, "custom_razorpay_key_id")

    key_secret = None
    webhook_secret = None
    if key_id:
        # Password fieldtype DB me encrypted hota hai — plain doc.field
        # se nahi milta, get_decrypted_password se hi padhna hota hai.
        from frappe.utils.password import get_decrypted_password
        key_secret = get_decrypted_password(
            "Company", company, "custom_razorpay_key_secret", raise_exception=False
        )
        webhook_secret = get_decrypted_password(
            "Company", company, "custom_razorpay_webhook_secret", raise_exception=False
        )

    # ---- Fallback: site_config.json (purana single/multi-account tarika,
    # sirf backward-compatibility ke liye — agar Company par kuch set
    # nahi hai to yeh use hoga) ----
    if not key_id:
        accounts = frappe.conf.get("razorpay_accounts") or {}
        acc = accounts.get(company) or {}
        key_id = acc.get("key_id") or frappe.conf.get("razorpay_key_id")
        key_secret = acc.get("key_secret") or frappe.conf.get("razorpay_key_secret")
        webhook_secret = acc.get("webhook_secret") or frappe.conf.get("razorpay_webhook_secret")

    creds = frappe._dict(key_id=key_id, key_secret=key_secret, webhook_secret=webhook_secret)
    if not creds.key_id or not creds.key_secret:
        frappe.throw(_("Razorpay is not configured for this trust"))
    return creds


def _order_payload(creds, order_id, amount_paise, company_name):
    # Donor ka PII (name/email/mobile) yahan se return nahi karte —
    # prefill frontend apne form se khud karta hai.
    return {
        "key_id": creds.key_id,
        "order_id": order_id,
        "amount": amount_paise,
        "currency": "INR",
        "name": company_name,
    }


# =========================================================================
# 1) ORDER CREATE
# =========================================================================
@frappe.whitelist(allow_guest=True)
def create_razorpay_order(sales_invoice):
    si = frappe.get_doc("Sales Invoice", sales_invoice)

    if si.docstatus != 1 or flt(si.outstanding_amount) <= 0 or not si.get("custom_donor_mobile"):
        frappe.throw(_("This invoice cannot be paid online"))

    creds = get_razorpay_credentials(si.company)
    auth = (creds.key_id, creds.key_secret)
    amount_paise = int(round(flt(si.grand_total) * 100))
    company_name = frappe.db.get_value("Company", si.company, "company_name") or si.company

    # Retry case: pehle wala order abhi payable hai to wahi reuse karo
    # (warna do orders me se pehle pe payment hone par verify fail hoga)
    existing_id = si.get("custom_razorpay_order_id")
    if existing_id:
        try:
            r = requests.get(ORDERS_URL + "/" + existing_id, auth=auth, timeout=15)
            if r.ok:
                od = r.json()
                if od.get("status") in ("created", "attempted") and od.get("amount") == amount_paise:
                    return _order_payload(creds, existing_id, amount_paise, company_name)
        except Exception:
            pass  # naya order bana lenge

    try:
        r = requests.post(
            ORDERS_URL,
            auth=auth,
            json={
                "amount": amount_paise,
                "currency": "INR",
                "receipt": si.name,
                "notes": {"sales_invoice": si.name, "trust": si.company},
            },
            timeout=15,
        )
        data = r.json()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Razorpay order request failed")
        frappe.throw(_("Payment gateway se contact nahi ho paya. Thodi der baad try karein."))

    if not r.ok or not data.get("id"):
        frappe.log_error(str(data), "Razorpay order create failed")
        frappe.throw(_("Payment shuru nahi ho paya. Thodi der baad try karein."))

    frappe.db.set_value(
        "Sales Invoice", si.name, "custom_razorpay_order_id", data["id"], update_modified=False
    )
    return _order_payload(creds, data["id"], amount_paise, company_name)


# =========================================================================
# 2) BROWSER CALLBACK VERIFY (Checkout ka handler yahan call karta hai)
#    signature = HMAC_SHA256(order_id + "|" + payment_id, key_secret)
# =========================================================================
@frappe.whitelist(allow_guest=True)
def verify_razorpay_payment(sales_invoice, razorpay_order_id, razorpay_payment_id, razorpay_signature):
    si = frappe.get_doc("Sales Invoice", sales_invoice)

    if not si.get("custom_razorpay_order_id") or si.custom_razorpay_order_id != razorpay_order_id:
        frappe.throw(_("Order does not match this invoice"))

    creds = get_razorpay_credentials(si.company)
    expected = hmac.new(
        creds.key_secret.encode("utf-8"),
        (razorpay_order_id + "|" + razorpay_payment_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, razorpay_signature or ""):
        frappe.log_error(
            "Invoice {0} order {1} payment {2}".format(si.name, razorpay_order_id, razorpay_payment_id),
            "Razorpay signature mismatch",
        )
        frappe.throw(_("Payment verification failed"))

    result = record_payment(si.name, razorpay_payment_id)
    result["status"] = "success"
    return result


# =========================================================================
# 3) WEBHOOK (Dashboard > Settings > Webhooks)
#    URL   : https://YOUR_SITE/api/method/<your_app>.api.razorpay_donation.razorpay_webhook
#    Events: payment.captured, order.paid
# =========================================================================
@frappe.whitelist(allow_guest=True)
def razorpay_webhook():
    raw = frappe.request.get_data()
    signature = frappe.get_request_header("X-Razorpay-Signature") or ""

    try:
        payload = json.loads(raw)
    except Exception:
        frappe.local.response["http_status_code"] = 400
        return {"status": "bad payload"}

    if payload.get("event") not in ("payment.captured", "order.paid"):
        return {"status": "ignored"}

    entity = ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
    order_id = entity.get("order_id")
    payment_id = entity.get("id")
    if not order_id or not payment_id:
        return {"status": "ignored"}

    si_name = frappe.db.get_value("Sales Invoice", {"custom_razorpay_order_id": order_id}, "name")
    if not si_name:
        return {"status": "unknown order"}

    company = frappe.db.get_value("Sales Invoice", si_name, "company")
    creds = get_razorpay_credentials(company)
    if not creds.webhook_secret:
        frappe.log_error("razorpay_webhook_secret not set", "Razorpay webhook")
        frappe.local.response["http_status_code"] = 400
        return {"status": "webhook secret missing"}

    expected = hmac.new(creds.webhook_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        frappe.log_error("Invalid webhook signature for " + si_name, "Razorpay webhook")
        frappe.local.response["http_status_code"] = 400
        return {"status": "invalid signature"}

    grand_total = frappe.db.get_value("Sales Invoice", si_name, "grand_total")
    if entity.get("amount") != int(round(flt(grand_total) * 100)):
        frappe.log_error("Amount mismatch for " + si_name, "Razorpay webhook")
        return {"status": "amount mismatch"}

    record_payment(si_name, payment_id)
    return {"status": "ok"}


# =========================================================================
# Payment Entry + receipt email
# Idempotent (verify aur webhook dono aa jaayein to bhi ek hi Payment Entry)
# aur Administrator context me chalta hai (Guest permission issues nahi aate).
# =========================================================================
def record_payment(si_name, reference_no):
    original_user = frappe.session.user
    frappe.set_user("Administrator")
    try:
        return _record_payment(si_name, reference_no)
    finally:
        frappe.set_user(original_user)


def _ensure_mode_of_payment(name="Online Payment"):
    if not frappe.db.exists("Mode of Payment", name):
        mop = frappe.new_doc("Mode of Payment")
        mop.mode_of_payment = name
        mop.type = "Bank"
        mop.insert(ignore_permissions=True)
    return name


def _record_payment(si_name, reference_no):
    # Row lock: webhook + browser callback ek saath aaye to duplicate na bane
    frappe.db.sql("SELECT name FROM `tabSales Invoice` WHERE name=%s FOR UPDATE", si_name)

    si = frappe.get_doc("Sales Invoice", si_name)
    print_format = frappe.db.get_value("Company", si.company, "custom_print_format") or "Standard"

    if flt(si.outstanding_amount) <= 0:
        existing = frappe.db.get_value(
            "Payment Entry Reference", {"reference_name": si.name, "docstatus": 1}, "parent"
        )
        return {
            "payment_entry": existing,
            "sales_invoice": si.name,
            "print_format": print_format,
            "payment_mode": "Online",
            "email_sent": False,
            "already_paid": True,
        }

    paid_to = (
        frappe.db.get_value("Company", si.company, "default_bank_account")
        or frappe.db.get_value("Company", si.company, "default_cash_account")
    )
    if not paid_to:
        frappe.throw(_("Company '{0}' me Default Bank/Cash Account set nahi hai").format(si.company))

    company_currency = frappe.db.get_value("Company", si.company, "default_currency")

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.company = si.company
    pe.party_type = "Customer"
    pe.party = si.customer
    pe.paid_amount = si.grand_total
    pe.received_amount = si.grand_total
    pe.paid_to = paid_to
    pe.reference_no = reference_no
    pe.reference_date = nowdate()
    pe.mode_of_payment = _ensure_mode_of_payment("Online Payment")
    pe.paid_from_account_currency = si.currency or company_currency
    pe.paid_to_account_currency = company_currency
    pe.source_exchange_rate = 1
    pe.target_exchange_rate = 1
    pe.append("references", {
        "reference_doctype": "Sales Invoice",
        "reference_name": si.name,
        "allocated_amount": si.grand_total,
    })
    pe.insert(ignore_permissions=True)
    pe.submit()
    frappe.db.commit()  # row lock release

    email_sent = _send_receipt_email(si, pe, print_format)

    return {
        "payment_entry": pe.name,
        "sales_invoice": si.name,
        "print_format": print_format,
        "payment_mode": "Online",
        "email_sent": email_sent,
    }


def _send_receipt_email(si, pe, print_format):
    # Email fail hone se payment record par asar nahi padna chahiye
    try:
        email = frappe.db.get_value("Customer", si.customer, "email_id")
        if not email:
            return False

        attachments = []
        try:
            pdf = frappe.get_print("Sales Invoice", si.name, print_format=print_format, as_pdf=True)
            attachments = [{"fname": si.name + ".pdf", "fcontent": pdf}]
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Donation receipt PDF failed: " + si.name)

        frappe.sendmail(
            recipients=[email],
            subject="Donation Receipt - {0} | {1}".format(si.name, si.company),
            message=(
                "<p>Dear {0},</p>"
                "<p>Thank you sincerely for your generous donation (Rs. {1}) to <b>{2}</b>.</p>"
                "<p>Your donation receipt is attached to this email.</p>"
                "<p>Payment Reference: {3}</p>"
                "<p>With gratitude,<br>{2}</p>"
            ).format(si.customer_name, si.grand_total, si.company, pe.reference_no),
            attachments=attachments,
            now=True,
        )
        return True
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Donation receipt email failed: " + si.name)
        return False

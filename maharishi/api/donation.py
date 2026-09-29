# -----------------------------------------------------------------------
# donation.py
#
# App path: maharishi/maharishi/api/donation.py
#
# Yeh 4 functions pehle Server Script (safe_exec) me the, ab poore app
# me — isliye normal Python: imports, .format(), frappe.generate_hash,
# sab allowed hai. Guest-permission gotchas (Contact auto-create,
# HRMS override, etc.) hamesha ke liye khatam karne ke liye har
# guest-facing function ko temporarily Administrator session me
# chalaya hai (frappe.set_user) — yeh Server Script sandbox me
# possible nahi tha, ab hai.
#
# Frontend CONFIG me dotted path use karna hoga (ab yeh Server Script
# nahi, app method hai):
#   API_METHOD:         "maharishi.api.donation.create_donation"
#   MAKE_PAYMENT_METHOD: "maharishi.api.donation.make_payment_1"
#   GET_TRUST_LIST_METHOD:   "maharishi.api.donation.get_trust_list"
#   GET_PURPOSE_LIST_METHOD: "maharishi.api.donation.get_purpose_list"
# -----------------------------------------------------------------------

import re

import frappe
from frappe import _
from frappe.utils import flt, nowdate, now


# =========================================================================
# Guest ke liye poora block Administrator session me chalane wala helper —
# doc.flags.ignore_permissions se zyada reliable hai (kisi bhi installed
# app ke internal hook/override ko bhi bypass kar deta hai).
# =========================================================================
def _run_as_admin(fn, *args, **kwargs):
    original_user = frappe.session.user
    frappe.set_user("Administrator")
    try:
        return fn(*args, **kwargs)
    finally:
        frappe.set_user(original_user)


# =========================================================================
# TRUST LIST
# =========================================================================
@frappe.whitelist(allow_guest=True)
def get_trust_list():
    return frappe.get_all(
        "Company",
        filters={},
        fields=["name", "company_name", "company_logo", "custom_lineage", "company_description"],
        order_by="company_name asc",
        ignore_permissions=True,
    )


# =========================================================================
# PURPOSE LIST
# =========================================================================
@frappe.whitelist(allow_guest=True)
def get_purpose_list(trust):
    if not trust:
        frappe.throw(_("Trust is required"))

    return frappe.get_all(
        "Donation Type",
        filters={"trust": trust},
        fields=["name", "donation_type"],
        order_by="donation_type asc",
        ignore_permissions=True,
    )


# =========================================================================
# CREATE DONATION — Customer create-or-fetch + Sales Invoice (submitted)
# =========================================================================
@frappe.whitelist(allow_guest=True)
def create_donation(donor_name, mobile, email, trust, amount, pan=None,
                     aadhaar=None, purpose=None, purpose_label=None):
    return _run_as_admin(
        _create_donation, donor_name, mobile, email, trust, amount,
        pan, aadhaar, purpose, purpose_label,
    )


def _create_donation(donor_name, mobile, email, trust, amount, pan,
                      aadhaar, purpose, purpose_label):
    mobile = (mobile or "").strip()
    pan = (pan or "").strip().upper()
    aadhaar = (aadhaar or "").strip()
    amount = flt(amount)

    if amount <= 0:
        frappe.throw(_("Invalid donation amount"))

    if not (mobile and len(mobile) == 10 and mobile.isdigit()):
        frappe.throw(_("Invalid mobile number"))

    if not trust or not frappe.db.exists("Company", trust):
        frappe.throw(_("Trust not found: {0}").format(trust))

    if not pan and not aadhaar:
        frappe.throw(_("PAN or Aadhaar is mandatory (at least one)"))

    if pan and not re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$", pan):
        frappe.throw(_("Invalid PAN format"))

    if aadhaar and not (len(aadhaar) == 12 and aadhaar.isdigit()):
        frappe.throw(_("Invalid Aadhaar format (12 digits required)"))

    # ---- Purpose ka trust se match hona chahiye (tampering-proof) ----
    if purpose:
        purpose_trust = frappe.db.get_value("Donation Type", purpose, "trust")
        if not purpose_trust or purpose_trust != trust:
            frappe.throw(_("Selected purpose does not belong to this trust"))

    # ---- Customer dedupe: PAN ya Aadhaar se (mobile se nahi) ----
    customer = None
    if pan:
        customer = frappe.db.get_value("Customer", {"pan": pan}, "name")
    if not customer and aadhaar:
        customer = frappe.db.get_value("Customer", {"custom_aadhaar": aadhaar}, "name")

    if not customer:
        cust = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": donor_name,
            "customer_type": "Individual",
            "mobile_no": mobile,
            "email_id": email,
            "pan": pan or None,
            "custom_aadhaar": aadhaar or None,
        })
        cust.insert(ignore_permissions=True)
        customer = cust.name
    else:
        cust_doc = frappe.get_doc("Customer", customer)
        cust_doc.mobile_no = mobile
        cust_doc.email_id = email
        cust_doc.save(ignore_permissions=True)

    si = frappe.get_doc({
        "doctype": "Sales Invoice",
        "customer": customer,
        "company": trust,
        "due_date": nowdate(),
        "is_pos": 0,
        "items": [{
            "item_code": "Donation",
            "qty": 1,
            "rate": amount,
            "description": purpose_label or "General Donation",
        }],
        "custom_donor_mobile": mobile,
        "custom_donor_pan": pan or None,
        "custom_donor_aadhaar": aadhaar or None,
        "custom_donation_purpose": purpose,
    })
    si.insert(ignore_permissions=True)
    si.submit()

    return {"sales_invoice": si.name}


# =========================================================================
# RECEIPT PDF — guest-safe (no login required). The Sales Invoice name
# alone is guessable (sequential naming), so this is protected by also
# requiring the donor's own mobile number to match what was recorded on
# the invoice. Streams the PDF directly instead of relying on /printview,
# which needs a Desk login + Read permission and silently fails for
# guest donors.
# =========================================================================
@frappe.whitelist(allow_guest=True)
def get_donation_receipt_pdf(sales_invoice, mobile=None):
    return _run_as_admin(_get_donation_receipt_pdf, sales_invoice, mobile)


def _get_donation_receipt_pdf(sales_invoice, mobile):
    si = frappe.get_doc("Sales Invoice", sales_invoice)
    mobile = (mobile or "").strip()

    if not mobile or si.get("custom_donor_mobile") != mobile:
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    print_format_name = frappe.db.get_value("Company", si.company, "custom_print_format") or "Standard"
    pdf = frappe.get_print("Sales Invoice", si.name, print_format=print_format_name, as_pdf=True)

    frappe.local.response.filename = si.name + ".pdf"
    frappe.local.response.filecontent = pdf
    frappe.local.response.type = "pdf"


# =========================================================================
# MAKE PAYMENT (Cash / Cheque / Online-demo) — Payment Entry + receipt email
# =========================================================================
@frappe.whitelist(allow_guest=True)
def make_payment_1(sales_invoice, payment_mode=None, cheque_no=None,
                    cheque_date=None, bank_name=None):
    return _run_as_admin(
        _make_payment_1, sales_invoice, payment_mode, cheque_no, cheque_date, bank_name
    )


def _make_payment_1(sales_invoice_name, payment_mode, cheque_no, cheque_date, bank_name):
    payment_mode = payment_mode or "Online"
    cheque_no = (cheque_no or "").strip()
    bank_name = (bank_name or "").strip()

    if not sales_invoice_name:
        frappe.throw(_("sales_invoice is required"))

    if payment_mode not in ("Online", "Cash", "Cheque"):
        frappe.throw(_("Invalid payment mode"))

    if payment_mode == "Cheque":
        if not cheque_no:
            frappe.throw(_("Cheque number is required"))
        if not cheque_date:
            frappe.throw(_("Cheque date is required"))

    si = frappe.get_doc("Sales Invoice", sales_invoice_name)
    print_format_name = frappe.db.get_value("Company", si.company, "custom_print_format") or "Standard"

    if si.outstanding_amount <= 0:
        existing_pe = frappe.db.get_value(
            "Payment Entry Reference",
            {"reference_name": si.name, "docstatus": 1},
            "parent"
        )
        return {
            "status": "already_paid",
            "payment_entry": existing_pe,
            "sales_invoice": si.name,
            "print_format": print_format_name,
        }

    # ---- Mode of Payment auto-create ----
    mode_of_payment_name = payment_mode if payment_mode != "Online" else "Online Payment"
    mop_type = "Cash" if payment_mode == "Cash" else "Bank"

    if not frappe.db.exists("Mode of Payment", mode_of_payment_name):
        frappe.get_doc({
            "doctype": "Mode of Payment",
            "mode_of_payment": mode_of_payment_name,
            "type": mop_type,
        }).insert(ignore_permissions=True)

    # ---- Paid To account ----
    if payment_mode == "Cash":
        default_account = frappe.db.get_value("Company", si.company, "default_cash_account")
    else:
        default_account = (
            frappe.db.get_value("Company", si.company, "default_bank_account")
            or frappe.db.get_value("Company", si.company, "default_cash_account")
        )

    if not default_account:
        frappe.throw(_(
            "Company '{0}' me Default {1} Account set nahi hai. "
            "Company master me Accounts Settings tab me set karein."
        ).format(si.company, "Cash" if payment_mode == "Cash" else "Bank"))

    company_currency = frappe.db.get_value("Company", si.company, "default_currency")

    # ---- Reference no / date ----
    if payment_mode == "Cheque":
        reference_no = cheque_no
        reference_date = cheque_date
    else:
        ts = now().replace("-", "").replace(":", "").replace(" ", "").replace(".", "")
        prefix = "CASH-" if payment_mode == "Cash" else "DEMO-"
        reference_no = prefix + ts
        reference_date = nowdate()

    pe_dict = {
        "doctype": "Payment Entry",
        "payment_type": "Receive",
        "company": si.company,
        "party_type": "Customer",
        "party": si.customer,
        "paid_amount": si.grand_total,
        "received_amount": si.grand_total,
        "paid_to": default_account,
        "reference_no": reference_no,
        "reference_date": reference_date,
        "mode_of_payment": mode_of_payment_name,
        "paid_from_account_currency": si.currency or company_currency,
        "paid_to_account_currency": company_currency,
        "source_exchange_rate": 1,
        "target_exchange_rate": 1,
        "references": [{
            "reference_doctype": "Sales Invoice",
            "reference_name": si.name,
            "allocated_amount": si.grand_total,
        }],
    }
    if payment_mode == "Cheque" and bank_name:
        pe_dict["custom_bank_name"] = bank_name

    pe = frappe.get_doc(pe_dict)
    pe.insert(ignore_permissions=True)
    pe.submit()

    email_sent, pdf_attached = _send_receipt_email(si, pe, payment_mode, print_format_name)

    return {
        "status": "success",
        "payment_entry": pe.name,
        "sales_invoice": si.name,
        "payment_mode": payment_mode,
        "email_sent": email_sent,
        "pdf_attached": pdf_attached,
        "print_format": print_format_name,
    }


def _send_receipt_email(si, pe, payment_mode, print_format_name):
    email_sent = False
    pdf_attached = False

    try:
        customer_email = frappe.db.get_value("Customer", si.customer, "email_id")
        if not customer_email:
            return email_sent, pdf_attached

        attachments = []
        try:
            pdf = frappe.get_print("Sales Invoice", si.name, print_format=print_format_name, as_pdf=True)
            attachments = [{"fname": si.name + ".pdf", "fcontent": pdf}]
            pdf_attached = True
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Donation receipt PDF failed: " + si.name)

        frappe.sendmail(
            recipients=[customer_email],
            subject="Donation Receipt - {0} | {1}".format(si.name, si.company),
            message=(
                "<p>Dear {0},</p>"
                "<p>Thank you sincerely for your generous donation (Rs. {1}) to <b>{2}</b>.</p>"
                "<p>Your donation receipt is attached to this email.</p>"
                "<p>Payment Mode: {3}</p>"
                "<p>Payment Reference: {4}</p>"
                "<p>With gratitude,<br>{2}</p>"
            ).format(si.customer_name, si.grand_total, si.company, payment_mode, pe.reference_no),
            attachments=attachments,
            now=True,
        )
        email_sent = True
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Donation receipt email failed: " + si.name)
        email_sent = False

    return email_sent, pdf_attached

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
from frappe.utils import flt, nowdate, now, getdate, escape_html
from frappe.rate_limiter import rate_limit
from frappe.contacts.doctype.address.address import get_address_display


# =========================================================================
# Guest ke liye poora block Administrator session me chalane wala helper —
# doc.flags.ignore_permissions se zyada reliable hai (kisi bhi installed
# app ke internal hook/override ko bhi bypass kar deta hai).
# =========================================================================
def _run_as_admin(fn, *args, **kwargs):
    """Run fn with Administrator permissions WITHOUT damaging the caller's session.

    frappe.set_user() is not a harmless switch. It overwrites session.sid with the
    user name, replaces session.data with an empty dict (dropping the CSRF token,
    IP, user agent ...) and wipes form_dict. Frappe then saves that damaged
    session back to the database/cache after the response. So everything
    set_user touches is snapshotted here and put back exactly, even if fn fails.
    """
    session = frappe.local.session
    saved_user, saved_sid, saved_data = session.user, session.sid, session.data
    saved_form_dict = frappe.local.form_dict

    frappe.set_user("Administrator")
    try:
        return fn(*args, **kwargs)
    finally:
        frappe.set_user(saved_user)
        session = frappe.local.session
        session.sid = saved_sid
        session.data = saved_data
        frappe.local.form_dict = saved_form_dict


# =========================================================================
# STAFF CHECK
# Counter (offline) payment modes may only be recorded by staff. Being merely
# "logged in" is not enough (portal/website users are logged in too), so we
# check for an accounts-type role. Override per site with:
#   bench set-config donation_staff_roles '["Accounts User","Donation Clerk"]' --parse
# =========================================================================
STAFF_ROLES = ("System Manager", "Accounts Manager", "Accounts User")


def _is_staff(user=None):
    user = user or frappe.session.user
    if user == "Guest":
        return False
    allowed = set(frappe.conf.get("donation_staff_roles") or STAFF_ROLES)
    return bool(allowed & set(frappe.get_roles(user)))


def _staff_required():
    """Raise the right error: anonymous callers learn nothing, but a logged-in
    user who lacks the role is told exactly what is missing."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    roles = ", ".join(sorted(set(frappe.conf.get("donation_staff_roles") or STAFF_ROLES)))
    frappe.throw(
        _("Your user ({0}) cannot record counter or in-kind donations. It needs one of these roles: {1}")
        .format(frappe.session.user, roles),
        frappe.PermissionError,
    )


@frappe.whitelist(allow_guest=True)
def is_donation_staff():
    """The donation page asks this once, so it only offers the counter payment
    modes to users the server would actually accept."""
    return _is_staff()


# =========================================================================
# PAYMENT MODES
#   mop        : Mode of Payment master name (auto-created if missing)
#   mop_type   : Mode of Payment "type"
#   account    : where the "Paid To" account comes from
#                  cash -> Company default cash account
#                  bank -> Company default bank account (falls back to cash)
#                  mop  -> Mode of Payment > Accounts table row for the company
#   ref_*      : bank-type modes need a reference number + date (cheque no,
#                UTR, ...). Others get an auto-generated reference.
# =========================================================================
PAYMENT_MODES = {
    "Online": {"mop": "Online Payment", "mop_type": "Bank", "account": "bank", "prefix": "DEMO-"},
    "Cash": {"mop": "Cash", "mop_type": "Cash", "account": "cash", "prefix": "CASH-"},
    "Cheque": {"mop": "Cheque", "mop_type": "Bank", "account": "bank",
               "ref_required": True, "ref_label": "Cheque number", "date_label": "Cheque date"},
    "RTGS": {"mop": "RTGS", "mop_type": "Bank", "account": "bank",
             "ref_required": True, "ref_label": "UTR / reference number", "date_label": "Transfer date"},
    "NEFT": {"mop": "NEFT", "mop_type": "Bank", "account": "bank",
             "ref_required": True, "ref_label": "UTR / reference number", "date_label": "Transfer date"},
    "Donation In Kind": {"mop": "Donation In Kind", "mop_type": "General", "account": "mop",
                         "prefix": "INKIND-"},
}


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
                     aadhaar=None, purpose=None, purpose_label=None,
                     address_line1=None, address_line2=None, city=None,
                     state=None, pincode=None, in_kind_details=None):
    # Decide "is this a staff member?" on the REAL caller, before
    # _run_as_admin swaps the session user to Administrator.
    is_staff = _is_staff()
    if (in_kind_details or "").strip() and not is_staff:
        _staff_required()
    return _run_as_admin(
        _create_donation, donor_name, mobile, email, trust, amount,
        pan, aadhaar, purpose, purpose_label,
        address_line1, address_line2, city, state, pincode,
        in_kind_details, is_staff,
    )


# =========================================================================
# DONOR LOOKUP — PAN/Aadhaar se pehle ka donor dhoondh ke naam + address
# wapas karta hai taaki form auto-fill ho sake.
#
# PRIVACY: yeh guest-accessible hai, isliye mobile/email kabhi wapas nahi
# karte (sirf masked hint) — warna koi bhi PAN guess karke kisi ka contact
# number nikal sakta hai. Rate-limit bhi laga hai (enumeration rokne ke liye).
# =========================================================================
def _mask_mobile(m):
    m = (m or "").strip()
    return (m[:2] + "xxxxxx" + m[-2:]) if len(m) >= 6 else ""


def _mask_email(e):
    e = (e or "").strip()
    if "@" not in e:
        return ""
    local, domain = e.split("@", 1)
    return (local[:2] + "***@" + domain) if local else ""


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=15, seconds=60 * 5)
def lookup_donor(pan=None, aadhaar=None):
    # Deliberately NOT run as Administrator: it only reads through frappe.db
    # (which does not check permissions anyway), and a guest-facing lookup has
    # no business running with elevated rights or touching the session.
    return _lookup_donor(pan, aadhaar)


def _lookup_donor(pan, aadhaar):
    pan = (pan or "").strip().upper()
    aadhaar = (aadhaar or "").strip()

    customer = None
    if pan and re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$", pan):
        customer = frappe.db.get_value("Customer", {"pan": pan}, "name")
    if not customer and aadhaar and len(aadhaar) == 12 and aadhaar.isdigit():
        customer = frappe.db.get_value("Customer", {"custom_aadhaar": aadhaar}, "name")

    if not customer:
        return {"found": False}

    cust = frappe.db.get_value(
        "Customer", customer,
        ["customer_name", "mobile_no", "email_id", "pan", "custom_aadhaar"],
        as_dict=True,
    )

    # Latest linked Address
    addr_name = frappe.db.sql(
        """SELECT a.name FROM `tabAddress` a
           JOIN `tabDynamic Link` dl ON dl.parent = a.name
             AND dl.parenttype = 'Address'
           WHERE dl.link_doctype = 'Customer' AND dl.link_name = %s
           ORDER BY a.modified DESC LIMIT 1""",
        customer,
    )
    address = {}
    if addr_name:
        address = frappe.db.get_value(
            "Address", addr_name[0][0],
            ["address_line1", "address_line2", "city", "state", "pincode"],
            as_dict=True,
        ) or {}

    return {
        "found": True,
        "donor_name": cust.customer_name,
        # Dusri identity bhi bhar dete hain (PAN diya to Aadhaar, ya ulta)
        "pan": cust.pan or "",
        "aadhaar": cust.custom_aadhaar or "",
        "address_line1": address.get("address_line1") or "",
        "address_line2": address.get("address_line2") or "",
        "city": address.get("city") or "",
        "state": address.get("state") or "",
        "pincode": address.get("pincode") or "",
        # Sirf masked hint — asli mobile/email donor ko dobara daalna hai
        "mobile_hint": _mask_mobile(cust.mobile_no),
        "email_hint": _mask_email(cust.email_id),
    }


def _get_or_create_address(customer, donor_name, address_line1, address_line2,
                           city, state, pincode):
    """Same address (line1 + pincode) pehle se linked ho to wahi use karo,
    warna naya Address banao aur Customer se link karo."""
    existing = frappe.db.sql(
        """SELECT a.name FROM `tabAddress` a
           JOIN `tabDynamic Link` dl ON dl.parent = a.name
             AND dl.parenttype = 'Address'
           WHERE dl.link_doctype = 'Customer' AND dl.link_name = %s
             AND a.address_line1 = %s AND IFNULL(a.pincode, '') = %s
           LIMIT 1""",
        (customer, address_line1, pincode),
    )
    if existing:
        return existing[0][0]

    addr = frappe.get_doc({
        "doctype": "Address",
        "address_title": donor_name,
        "address_type": "Billing",
        "address_line1": address_line1,
        "address_line2": address_line2 or None,
        "city": city,
        "state": state,
        "pincode": pincode,
        "country": "India",
        "links": [{"link_doctype": "Customer", "link_name": customer}],
    })
    addr.insert(ignore_permissions=True)
    return addr.name


def _create_donation(donor_name, mobile, email, trust, amount, pan,
                      aadhaar, purpose, purpose_label,
                      address_line1, address_line2, city, state, pincode,
                      in_kind_details=None, is_staff=False):
    in_kind_details = (in_kind_details or "").strip()
    if in_kind_details and not is_staff:
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if len(in_kind_details) > 300:
        frappe.throw(_("In-kind details are too long (max 300 characters)"))

    mobile = (mobile or "").strip()
    pan = (pan or "").strip().upper()
    aadhaar = (aadhaar or "").strip()
    address_line1 = (address_line1 or "").strip()
    address_line2 = (address_line2 or "").strip()
    city = (city or "").strip()
    state = (state or "").strip()
    pincode = (pincode or "").strip()
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

    # ---- Address (client requirement: proper donor address) ----
    if not (address_line1 and city and state):
        frappe.throw(_("Address line 1, city and state are required"))
    if not (len(pincode) == 6 and pincode.isdigit()):
        frappe.throw(_("Invalid pincode (6 digits required)"))

    # ---- In-kind: make sure the booking account exists BEFORE creating anything.
    # Otherwise the invoice would be created and then the payment step would
    # fail, leaving an unpaid invoice behind. ----
    if in_kind_details:
        _resolve_paid_to(PAYMENT_MODES["Donation In Kind"], trust)

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
        # Existing donor: mobile/email hamesha latest wale se update
        # (donor ne naya daala ho sakta hai), PAN/Aadhaar missing ho to bhar do
        cust_doc = frappe.get_doc("Customer", customer)
        cust_doc.mobile_no = mobile
        cust_doc.email_id = email
        if pan and not cust_doc.get("pan"):
            cust_doc.pan = pan
        if aadhaar and not cust_doc.get("custom_aadhaar"):
            cust_doc.custom_aadhaar = aadhaar
        cust_doc.save(ignore_permissions=True)

    address_name = _get_or_create_address(
        customer, donor_name, address_line1, address_line2, city, state, pincode
    )

    # Print format me doc.address_display use hota hai — explicitly set karo
    address_display = get_address_display(address_name)

    # The receipt prints the item description, so an in-kind gift is described
    # right there (e.g. "General Donation - In kind: 5 kg atta").
    description = purpose_label or "General Donation"
    if in_kind_details:
        description += " - In kind: " + in_kind_details

    si = frappe.get_doc({
        "doctype": "Sales Invoice",
        "customer": customer,
        "company": trust,
        "due_date": nowdate(),
        "is_pos": 0,
        "customer_address": address_name,
        "address_display": address_display,
        "items": [{
            "item_code": "Donation",
            "qty": 1,
            "rate": amount,
            "description": description,
        }],
        "custom_in_kind_details": in_kind_details or None,
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
                    cheque_date=None, bank_name=None, in_kind_details=None):
    # NOTE: cheque_no / cheque_date / bank_name are also used for RTGS and NEFT
    # (UTR number, transfer date, bank) - the names are kept for compatibility.
    payment_mode = payment_mode or "Online"
    if payment_mode not in PAYMENT_MODES:
        frappe.throw(_("Invalid payment mode"))

    # Authorisation uses the REAL caller (before the admin switch below).
    # Without this check anybody on the internet could mark any invoice as paid.
    if payment_mode == "Online":
        # Online payments are confirmed by the gateway (verify_razorpay_payment /
        # webhook), never by the browser. This path exists only for the
        # no-gateway demo and must be switched on explicitly:
        #   bench set-config donation_demo_mode 1
        if not frappe.conf.get("donation_demo_mode"):
            frappe.throw(_("Not permitted"), frappe.PermissionError)
    elif not _is_staff():
        _staff_required()

    return _run_as_admin(
        _make_payment_1, sales_invoice, payment_mode, cheque_no, cheque_date,
        bank_name, in_kind_details,
    )


def _resolve_paid_to(cfg, company):
    """Return the account the money (or the goods) is booked to."""
    if cfg["account"] == "cash":
        account = frappe.db.get_value("Company", company, "default_cash_account")
        missing = "a Default Cash Account"
    elif cfg["account"] == "bank":
        account = (
            frappe.db.get_value("Company", company, "default_bank_account")
            or frappe.db.get_value("Company", company, "default_cash_account")
        )
        missing = "a Default Bank Account"
    else:
        account = frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": cfg["mop"], "company": company},
            "default_account",
        )
        missing = "an account set under Mode of Payment '{0}' > Accounts (add a row for this company)".format(cfg["mop"])

    if not account:
        frappe.throw(_("Company '{0}' is missing {1}. Please set it up first.").format(company, missing))
    return account


def _make_payment_1(sales_invoice_name, payment_mode, cheque_no, cheque_date,
                    bank_name, in_kind_details):
    cfg = PAYMENT_MODES[payment_mode]
    reference_no = (cheque_no or "").strip()
    bank_name = (bank_name or "").strip()
    reference_date = cheque_date

    if not sales_invoice_name:
        frappe.throw(_("sales_invoice is required"))

    # ---- Reference number / date ----
    if cfg.get("ref_required"):
        if not reference_no:
            frappe.throw(_("{0} is required").format(cfg["ref_label"]))
        if not reference_date:
            frappe.throw(_("{0} is required").format(cfg["date_label"]))
        reference_date = getdate(reference_date)  # raises on a malformed date
    else:
        ts = now().replace("-", "").replace(":", "").replace(" ", "").replace(".", "")
        reference_no = cfg["prefix"] + ts
        reference_date = nowdate()

    si = frappe.get_doc("Sales Invoice", sales_invoice_name)
    print_format_name = frappe.db.get_value("Company", si.company, "custom_print_format") or "Standard"

    # ---- Donation in kind: what was donated must be on record ----
    details = ""
    if payment_mode == "Donation In Kind":
        details = (si.get("custom_in_kind_details") or in_kind_details or "").strip()
        if not details:
            frappe.throw(_("Please describe what the donor gave (item and quantity)"))

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
    if not frappe.db.exists("Mode of Payment", cfg["mop"]):
        frappe.get_doc({
            "doctype": "Mode of Payment",
            "mode_of_payment": cfg["mop"],
            "type": cfg["mop_type"],
        }).insert(ignore_permissions=True)

    paid_to = _resolve_paid_to(cfg, si.company)
    company_currency = frappe.db.get_value("Company", si.company, "default_currency")

    pe_dict = {
        "doctype": "Payment Entry",
        "payment_type": "Receive",
        "company": si.company,
        "party_type": "Customer",
        "party": si.customer,
        "paid_amount": si.grand_total,
        "received_amount": si.grand_total,
        "paid_to": paid_to,
        "reference_no": reference_no,
        "reference_date": reference_date,
        "mode_of_payment": cfg["mop"],
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
    if cfg.get("ref_required") and bank_name:
        pe_dict["custom_bank_name"] = bank_name
    if payment_mode == "Donation In Kind":
        pe_dict["remarks"] = "Donation in kind: " + details

    pe = frappe.get_doc(pe_dict)
    pe.insert(ignore_permissions=True)
    pe.submit()

    email_sent, pdf_attached = _send_receipt_email(si, pe, payment_mode, print_format_name, details)

    return {
        "status": "success",
        "payment_entry": pe.name,
        "sales_invoice": si.name,
        "payment_mode": payment_mode,
        "email_sent": email_sent,
        "pdf_attached": pdf_attached,
        "print_format": print_format_name,
    }


def _send_receipt_email(si, pe, payment_mode, print_format_name, in_kind_details=""):
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
            message=_receipt_email_body(si, pe, payment_mode, in_kind_details),
            attachments=attachments,
            now=True,
        )
        email_sent = True
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Donation receipt email failed: " + si.name)
        email_sent = False

    return email_sent, pdf_attached


def _receipt_email_body(si, pe, payment_mode, in_kind_details):
    if payment_mode == "Donation In Kind":
        intro = (
            "<p>Thank you sincerely for your generous in-kind donation of "
            "<b>{0}</b> to <b>{1}</b>, recorded at an equivalent value of Rs. {2}.</p>"
        ).format(escape_html(in_kind_details), escape_html(si.company), si.grand_total)
        extra = ""
    else:
        intro = (
            "<p>Thank you sincerely for your generous donation (Rs. {0}) to <b>{1}</b>.</p>"
        ).format(si.grand_total, escape_html(si.company))
        extra = "<p>Payment Mode: {0}</p><p>Payment Reference: {1}</p>".format(
            escape_html(payment_mode), escape_html(pe.reference_no)
        )

    return (
        "<p>Dear {0},</p>{1}<p>Your donation receipt is attached to this email.</p>{2}"
        "<p>With gratitude,<br>{3}</p>"
    ).format(escape_html(si.customer_name), intro, extra, escape_html(si.company))

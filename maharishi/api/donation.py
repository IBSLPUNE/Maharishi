

import re
import hashlib
import requests
import frappe
from frappe import _
from frappe.utils import flt, nowdate, get_url


DONATION_ITEM = "Donation"   

@frappe.whitelist(allow_guest=True)
def get_trust_list():
  
    try:
        filters = {}
       

        companies = frappe.get_all(
            "Company",
            filters=filters,
            fields=[
                "name",
                "company_name",
                "company_logo",
                "custom_lineage",
                "company_description"
            ],
            order_by="company_name asc",
            ignore_permissions=True
        )
        return companies

    except Exception:
        frappe.log_error(title="Donation Page - get_trust_list", message=frappe.get_traceback())
        frappe.throw(_("Unable to fetch trust list right now. Please try again later."))


@frappe.whitelist(allow_guest=True)
def get_purpose_list(trust):
   
    if not trust:
        frappe.throw(_("Trust is required"))

    try:
        types = frappe.get_all(
            "Donation Type",
            filters={"trust": trust},
            fields=["name", "donation_type"],
            order_by="donation_type asc",
            ignore_permissions=True
        )
        return types

    except Exception:
        frappe.log_error(title="Donation Page - get_purpose_list", message=frappe.get_traceback())
        frappe.throw(_("Unable to fetch purpose list right now. Please try again later."))


def get_easebuzz_settings():
  
    key = frappe.conf.get("easebuzz_key")
    salt = frappe.conf.get("easebuzz_salt")
    if not key or not salt:
        return None
    return frappe._dict({
        "key": key,
        "salt": salt,
        "env": frappe.conf.get("easebuzz_env") or "test",
    })


def easebuzz_base_url(env):
    return "https://pay.easebuzz.in/" if env == "prod" else "https://testpay.easebuzz.in/"


@frappe.whitelist(allow_guest=True)
def create_donation(trust, trust_display_name, donor_name, mobile, email,
                     pan=None, purpose=None, purpose_label=None, amount=0):
   
    amount = flt(amount)
    if amount <= 0:
        frappe.throw(_("Invalid donation amount"))

    if not (mobile and len(mobile) == 10 and mobile.isdigit()):
        frappe.throw(_("Invalid mobile number"))

    if not frappe.db.exists("Company", trust):
        frappe.throw(_("Trust not found: {0}").format(trust))

    if pan:
        pan = pan.strip().upper()
        if not re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$", pan):
            frappe.throw(_("Invalid PAN format"))

   
    if purpose:
        purpose_trust = frappe.db.get_value("Donation Type", purpose, "trust")
        if not purpose_trust:
            frappe.throw(_("Invalid purpose"))
        if purpose_trust != trust:
            frappe.throw(_("Selected purpose does not belong to this trust"))

    customer = get_or_create_donor_customer(donor_name, mobile, email, pan)

   
    si = frappe.new_doc("Sales Invoice")
    si.customer = customer
    si.company = trust
    si.due_date = nowdate()
    si.is_pos = 0
    si.append("items", {
        "item_code": DONATION_ITEM,
        "qty": 1,
        "rate": amount,
        "description": purpose_label or "General Donation",
    })
    si.custom_donor_mobile = mobile

    si.custom_donor_pan = pan
    si.custom_donation_purpose = purpose
    si.insert(ignore_permissions=True)   # draft

 
    settings = get_easebuzz_settings()

    if settings:
        try:
            payment_url = get_easebuzz_checkout_url(si, amount, donor_name, mobile, email, settings)
        except Exception:
            frappe.delete_doc("Sales Invoice", si.name, ignore_permissions=True, force=True)
            frappe.log_error(frappe.get_traceback(), "Easebuzz checkout URL failed")
            frappe.throw(_("Payment is starting. Try after some time please."))

        si.submit()
        return {"sales_invoice": si.name, "payment_url": payment_url, "test_mode": False}

    elif frappe.conf.get("developer_mode"):
      
        si.submit()
        return {"sales_invoice": si.name, "payment_url": None, "test_mode": True}

    else:
        frappe.delete_doc("Sales Invoice", si.name, ignore_permissions=True, force=True)
        frappe.throw(_(
            "Payment gateway abhi configure nahi hai. Admin se site_config.json "
            "me 'easebuzz_key' aur 'easebuzz_salt' set karne ko kahein."
        ))


def get_or_create_donor_customer(donor_name, mobile, email, pan):
    existing = frappe.db.get_value("Customer", {"mobile_no": mobile}, "name")
    if existing:
        return existing

    cust = frappe.new_doc("Customer")
    cust.customer_name = donor_name
    cust.customer_type = "Individual"
    cust.mobile_no = mobile
    cust.email_id = email
    if pan:
        cust.pan = pan
    cust.insert(ignore_permissions=True)
    return cust.name
 
def get_easebuzz_checkout_url(sales_invoice, amount, donor_name, mobile, email, settings=None):
    settings = settings or get_easebuzz_settings()
    if not settings:
        frappe.throw(_("Easebuzz key/salt site_config.json me set nahi hain"))
    base_url = easebuzz_base_url(settings.env)

    txnid = sales_invoice.name.replace("-", "")[:20] + "-" + frappe.generate_hash(length=6)
    productinfo = "Donation"
    udf1 = sales_invoice.name   # webhook/SURL isi se invoice identify karta hai

    frappe.db.set_value("Sales Invoice", sales_invoice.name, "custom_easebuzz_txnid", txnid)

    hash_string = "|".join([
        settings.key, txnid, str(amount), productinfo, donor_name, email,
        udf1, "", "", "", "", "", "",   # udf1..udf7
        "", "", "",                      # udf8, udf9, udf10
        settings.salt
    ])
    generated_hash = hashlib.sha512(hash_string.encode("utf-8")).hexdigest()

    payload = {
        "key": settings.key,
        "txnid": txnid,
        "amount": str(amount),
        "productinfo": productinfo,
        "firstname": donor_name,
        "phone": mobile,
        "email": email,
        "surl": f"{get_url()}/api/method/maharshi.api.donation.payment_response",
        "furl": f"{get_url()}/api/method/maharshi.api.donation.payment_response",
        "hash": generated_hash,
        "udf1": udf1,
    }

    try:
        r = requests.post(base_url + "payment/initiateLink", data=payload, timeout=15)
        res = r.json()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Easebuzz Initiate Payment - Request Failed")
        frappe.throw(_("Payment gateway se contact nahi ho paya. Baad me try karein."))

    if res.get("status") != 1:
        frappe.log_error(str(res), "Easebuzz Initiate Payment Failed")
        frappe.throw(_("Payment shuru nahi ho paya: {0}").format(res.get("data")))

    access_key = res.get("data")
    return f"{base_url}pay/{access_key}"

 
def verify_easebuzz_response(data):
    """
    data: Easebuzz se aaya hua form_dict (SURL/FURL ya webhook dono se).
    Return: (is_valid: bool, sales_invoice_name: str|None, reason: str)
    """
    settings = get_easebuzz_settings()
    if not settings:
        return False, None, "Easebuzz not configured on this site"

    status = (data.get("status") or "").lower()
    txnid = data.get("txnid")
    amount = data.get("amount")
    productinfo = data.get("productinfo")
    firstname = data.get("firstname")
    email = data.get("email")
    udf1 = data.get("udf1")            
    received_hash = data.get("hash")

    if not (txnid and received_hash and udf1):
        return False, None, "Missing mandatory fields in gateway response"

    reverse_hash_string = "|".join([
        settings.salt, status,
        "", "", "",                     
        "", "", "", "", "", "",         
        udf1,
        email or "", firstname or "", productinfo or "", str(amount or ""), txnid,
        settings.key
    ])
    expected_hash = hashlib.sha512(reverse_hash_string.encode("utf-8")).hexdigest()

    if expected_hash != received_hash:
        return False, None, "Hash mismatch — response tampered or invalid"

    expected_txnid = frappe.db.get_value("Sales Invoice", udf1, "custom_easebuzz_txnid")
    if expected_txnid and expected_txnid != txnid:
        return False, None, f"txnid mismatch for {udf1}"

    invoice_amount = frappe.db.get_value("Sales Invoice", udf1, "grand_total")
    if invoice_amount is None:
        return False, None, f"Sales Invoice not found: {udf1}"
    if abs(flt(amount) - flt(invoice_amount)) > 0.01:
        return False, None, f"Amount mismatch for {udf1}: gateway={amount} invoice={invoice_amount}"

    if status != "success":
        return True, None, f"Payment status = {status}"

    return True, udf1, "OK"


@frappe.whitelist(allow_guest=True)
def payment_response():
    
    data = frappe.local.form_dict
    is_valid, sales_invoice_name, reason = verify_easebuzz_response(data)

    if not is_valid:
        frappe.log_error(f"{reason} | data={dict(data)}", "Easebuzz Response Verification Failed")
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = "/donation-failed"
        return

    if sales_invoice_name:
        mark_donation_paid(
            sales_invoice_name=sales_invoice_name,
            gateway_txn_id=data.get("txnid"),
            gateway_payment_id=data.get("easepayid") or data.get("txnid"),
        )
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = f"/donation-success?invoice={sales_invoice_name}"
    else:
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = "/donation-failed"


@frappe.whitelist(allow_guest=True)
def easebuzz_webhook():
    
    data = frappe.local.form_dict
    is_valid, sales_invoice_name, reason = verify_easebuzz_response(data)

    if not is_valid:
        frappe.log_error(f"{reason} | data={dict(data)}", "Easebuzz Webhook Verification Failed")
        frappe.local.response["http_status_code"] = 400
        return {"status": "rejected", "reason": reason}

    if sales_invoice_name:
        mark_donation_paid(
            sales_invoice_name=sales_invoice_name,
            gateway_txn_id=data.get("txnid"),
            gateway_payment_id=data.get("easepayid") or data.get("txnid"),
        )

    return {"status": "ok"}


 
def ensure_mode_of_payment(mode_name="Online Payment"):
    """Mode of Payment master agar exist nahi karta to bana deta hai —
    taaki fresh site par yeh setup step manually na karna pade."""
    if not frappe.db.exists("Mode of Payment", mode_name):
        mop = frappe.new_doc("Mode of Payment")
        mop.mode_of_payment = mode_name
        mop.type = "General"
        mop.insert(ignore_permissions=True)
    return mode_name



def mark_donation_paid(sales_invoice_name, gateway_txn_id, gateway_payment_id):
    
    original_user = frappe.session.user
    frappe.set_user("Administrator")
    try:
        return _mark_donation_paid(sales_invoice_name, gateway_txn_id, gateway_payment_id)
    finally:
        frappe.set_user(original_user)

def _mark_donation_paid(sales_invoice_name, gateway_txn_id, gateway_payment_id):

    frappe.db.sql(
        "SELECT name FROM `tabSales Invoice` WHERE name=%s FOR UPDATE",
        sales_invoice_name
    )

    si = frappe.get_doc("Sales Invoice", sales_invoice_name)

    if si.outstanding_amount <= 0:
        return frappe.db.get_value(
            "Payment Entry Reference",
            {"reference_name": si.name, "docstatus": 1},
            "parent"
        )  

    default_account = (
        frappe.db.get_value("Company", si.company, "default_bank_account")
        or frappe.db.get_value("Company", si.company, "default_cash_account")
    )
    if not default_account:
        frappe.throw(_(
            "Company '{0}' me Default Cash/Bank Account not set. "
            "Company master , Set Accounts Settings tab ."
        ).format(si.company))

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.company = si.company
    pe.party_type = "Customer"
    pe.party = si.customer
    pe.paid_amount = si.grand_total
    pe.received_amount = si.grand_total
    pe.paid_to = default_account
    pe.reference_no = gateway_payment_id
    pe.reference_date = nowdate()
    pe.mode_of_payment = ensure_mode_of_payment("Online Payment")

    
    company_currency = frappe.db.get_value("Company", si.company, "default_currency")
    pe.paid_from_account_currency = si.currency or company_currency
    pe.paid_to_account_currency = company_currency
    pe.source_exchange_rate = 1
    pe.target_exchange_rate = 1
    pe.append("references", {
        "reference_doctype": "Sales Invoice",
        "reference_name": si.name,
        "allocated_amount": si.grand_total
    })
    pe.insert(ignore_permissions=True)
    pe.submit()

    frappe.db.commit()   

    try:
        send_donation_receipt_email(si, pe)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Donation receipt email failed: {si.name}")

    return pe.name


def send_donation_receipt_email(sales_invoice, payment_entry):
    
   
    customer_email = frappe.db.get_value("Customer", sales_invoice.customer, "email_id")
    if not customer_email:
        frappe.log_error(f"No email for customer {sales_invoice.customer}", "Donation receipt skipped")
        return

    pdf = frappe.get_print(
        "Sales Invoice", sales_invoice.name,
        print_format="Standard",   # apna custom "Donation Receipt" print format bana sakte hain
        as_pdf=True
    )

    frappe.sendmail(
        recipients=[customer_email],
        subject=f"Donation Slip — {sales_invoice.name} | {sales_invoice.company}",
        message = f"""
            <p>Dear {sales_invoice.customer_name},</p>
            <p>Thank you sincerely for your generous donation (&#8377;{sales_invoice.grand_total}) to
            <b>{sales_invoice.company}</b>.</p>
            <p>Your donation receipt is attached to this email.</p>
            <p>Payment Reference: {payment_entry.reference_no}</p>
            <p>With gratitude,<br>{sales_invoice.company}</p>
        """,

        attachments=[{
            "fname": f"{sales_invoice.name}.pdf",
            "fcontent": pdf
        }],
        now=True,
    )

 
@frappe.whitelist(allow_guest=True)
def simulate_payment_success(sales_invoice):
    if not frappe.conf.get("developer_mode"):
        frappe.throw(_("Test mode sirf developer_mode me allowed hai"))

    pe_name = mark_donation_paid(
        sales_invoice_name=sales_invoice,
        gateway_txn_id="TEST-" + frappe.generate_hash(length=8),
        gateway_payment_id="TEST-SIMULATED"
    )
    return {"status": "success", "payment_entry": pe_name}

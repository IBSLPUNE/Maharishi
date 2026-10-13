# Razorpay Settlement Report  (Script Report)
#
# How much of the money donors paid through Razorpay has already been settled
# to the bank and how much is still pending - trust by trust, because every
# trust has its own Razorpay account (keys are read from the Company form).
#
# Views
#   Summary (Trust-wise) : one line per trust - settled / pending / fees / refunds
#   Payment-wise         : every captured payment with its settlement status
#   Settlement-wise      : the actual bank settlements (match with bank statement)
#
# How "settled" is decided (Razorpay API facts, from their docs)
#   * GET /v1/payments                      -> every payment in the date range
#                                              (amount, fee incl. GST, status ...)
#   * GET /v1/settlements/recon/combined    -> transactions that HAVE been settled,
#                                              looked up by SETTLEMENT date
#   A captured payment that does not appear in the recon data is "Pending".
#
# Extra reconciliation columns
#   * Payment Entry / ERPNext : is there a Payment Entry whose reference_no is the
#     Razorpay payment id?  "Missing in ERPNext" = money captured but not booked.
#
# Credentials never leave the server. Nothing is written back to Razorpay.

import datetime
import time

import frappe
import requests
from frappe import _
from frappe.utils import escape_html, flt, getdate, nowdate
from frappe.utils.password import get_decrypted_password

API = "https://api.razorpay.com/v1"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

PAGE = 100            # max page size for payments / settlements
RECON_PAGE = 1000     # max page size for recon
MAX_PAGES = 300       # safety stop per endpoint
MAX_DAYS = 366        # longest date range allowed
RECON_LAG_MONTHS = 2  # how far past "To Date" we look for settlements
CHUNK = 400

VIEW_SUMMARY = "Summary (Trust-wise)"
VIEW_PAYMENT = "Payment-wise"
VIEW_SETTLEMENT = "Settlement-wise"

SETTLED = "Settled"
PENDING = "Pending Settlement"
REFUNDED = "Refunded"
AUTHORIZED = "Authorized (not captured)"

ERP_RECORDED = "Recorded"
ERP_MISSING = "Missing in ERPNext"
ERP_REFUND_GAP = "Refunded - not in ERPNext"
ERP_NA = "-"


class RazorpayError(Exception):
    pass


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def execute(filters=None):
    filters = frappe._dict(filters or {})
    from_date, to_date = _validate(filters)
    view = filters.get("view") or VIEW_SUMMARY

    companies, warnings = _select_companies(filters.get("company"))
    from_ts, to_ts = _day_bounds(from_date, to_date)

    payment_rows, settlement_rows = [], []
    for company, creds in companies:
        if creds.key_id.startswith("rzp_test_"):
            warnings.append(_("{0}: TEST keys are configured - this is test data, not real money.").format(company))
        try:
            if view == VIEW_SETTLEMENT:
                settlement_rows += _settlement_rows(company, creds, from_ts, to_ts)
            else:
                payment_rows += _payment_rows(company, creds, from_date, to_date, from_ts, to_ts)
        except RazorpayError as e:
            warnings.append(str(e))

    message = "<br>".join(escape_html(w) for w in warnings) or None

    if view == VIEW_SETTLEMENT:
        settlement_rows.sort(key=lambda r: (r["date"], r["settlement_id"]), reverse=True)
        return _settlement_columns(), settlement_rows, message, None, _settlement_summary(settlement_rows)

    if view == VIEW_PAYMENT:
        status = filters.get("status")
        rows = [r for r in payment_rows if _matches_status(r, status)]
        rows.sort(key=lambda r: (r["date"], r["payment_id"]), reverse=True)
        return _payment_columns(), rows, message, None, _payment_summary(payment_rows)

    return _summary_columns(), _summary_rows(payment_rows), message, None, _payment_summary(payment_rows)


# --------------------------------------------------------------------------- #
# validation / company selection / credentials
# --------------------------------------------------------------------------- #
def _validate(filters):
    if not filters.get("from_date") or not filters.get("to_date"):
        frappe.throw(_("From Date and To Date are required"))
    from_date, to_date = getdate(filters.from_date), getdate(filters.to_date)
    if from_date > to_date:
        frappe.throw(_("From Date cannot be after To Date"))
    if (to_date - from_date).days > MAX_DAYS:
        frappe.throw(_("Please choose a date range of at most {0} days").format(MAX_DAYS))
    return from_date, to_date


def _credentials(company):
    """Same sources as maharishi.api.razorpay_donation: Company form first, then site_config."""
    key_id = frappe.db.get_value("Company", company, "custom_razorpay_key_id")
    key_secret = webhook = None
    if key_id:
        key_secret = get_decrypted_password("Company", company, "custom_razorpay_key_secret", raise_exception=False)
    else:
        acc = (frappe.conf.get("razorpay_accounts") or {}).get(company) or {}
        key_id = acc.get("key_id") or frappe.conf.get("razorpay_key_id")
        key_secret = acc.get("key_secret") or frappe.conf.get("razorpay_key_secret")
    if not key_id or not key_secret:
        return None
    return frappe._dict(key_id=key_id, key_secret=key_secret)


def _select_companies(company):
    warnings, selected = [], []
    names = [company] if company else frappe.get_all("Company", pluck="name", order_by="name asc")
    skipped = []
    for name in names:
        creds = _credentials(name)
        if creds:
            selected.append((name, creds))
        else:
            skipped.append(name)
    if skipped:
        warnings.append(_("No Razorpay keys configured for: {0}").format(", ".join(skipped)))
    return selected, warnings


# --------------------------------------------------------------------------- #
# Razorpay HTTP
# --------------------------------------------------------------------------- #
def _get(company, creds, path, params):
    last_problem = "request failed"
    for attempt in range(3):
        try:
            r = requests.get(API + path, params=params, auth=(creds.key_id, creds.key_secret), timeout=30)
        except requests.RequestException as e:
            last_problem = "could not reach Razorpay ({0})".format(type(e).__name__)
            time.sleep(1)
            continue
        if r.status_code == 429:
            last_problem = "Razorpay rate limit hit, please retry in a minute"
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r.json()
        description = ""
        try:
            description = (r.json().get("error") or {}).get("description") or ""
        except ValueError:
            pass
        raise RazorpayError("{0}: Razorpay said \"{1}\" (HTTP {2})".format(
            company, description or "request failed", r.status_code))
    raise RazorpayError("{0}: {1}".format(company, last_problem))


def _paginate(company, creds, path, base_params, page_size):
    items, skip = [], 0
    for _page in range(MAX_PAGES):
        params = dict(base_params, count=page_size, skip=skip)
        batch = _get(company, creds, path, params).get("items") or []
        items.extend(batch)
        if len(batch) < page_size:
            return items
        skip += page_size
    raise RazorpayError("{0}: too many records - please choose a shorter date range".format(company))


# --------------------------------------------------------------------------- #
# dates / money
# --------------------------------------------------------------------------- #
def _day_bounds(from_date, to_date):
    start = datetime.datetime.combine(from_date, datetime.time.min, tzinfo=IST)
    end = datetime.datetime.combine(to_date, datetime.time(23, 59, 59), tzinfo=IST)
    return int(start.timestamp()), int(end.timestamp())


def _ts_date(ts):
    return datetime.datetime.fromtimestamp(int(ts), IST).date()


def _rupees(paise):
    return round(flt(paise) / 100.0, 2)


def _year_month_add(year, month, delta):
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def _recon_months(from_date, to_date):
    today = getdate(nowdate())
    first = (from_date.year, from_date.month)
    last = min((today.year, today.month), _year_month_add(to_date.year, to_date.month, RECON_LAG_MONTHS))
    months, cur = [], first
    while cur <= last:
        months.append(cur)
        cur = _year_month_add(cur[0], cur[1], 1)
    return months


# --------------------------------------------------------------------------- #
# per-company data
# --------------------------------------------------------------------------- #
def _fetch_recon(company, creds, from_date, to_date):
    """payment_id -> settlement details, for payments that HAVE been settled."""
    settled = {}
    for year, month in _recon_months(from_date, to_date):
        for item in _paginate(company, creds, "/settlements/recon/combined",
                              {"year": year, "month": month}, RECON_PAGE):
            if item.get("type") == "payment" and item.get("settled"):
                settled[item.get("entity_id")] = item
    return settled


def _chunks(seq, n=CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _erp_lookup(payment_ids, invoice_names):
    """Razorpay payment id -> Payment Entry, and invoice -> donor name."""
    entries, donors = {}, {}
    for chunk in _chunks(sorted(payment_ids)):
        ph = ", ".join(["%s"] * len(chunk))
        for r in frappe.db.sql(
            "SELECT reference_no, name FROM `tabPayment Entry` "
            "WHERE docstatus = 1 AND reference_no IN ({0})".format(ph), tuple(chunk), as_dict=True):
            entries[r.reference_no] = r.name
    for chunk in _chunks(sorted(invoice_names)):
        ph = ", ".join(["%s"] * len(chunk))
        for r in frappe.db.sql(
            "SELECT name, customer_name FROM `tabSales Invoice` WHERE name IN ({0})".format(ph),
            tuple(chunk), as_dict=True):
            donors[r.name] = r.customer_name
    return entries, donors


def _payment_rows(company, creds, from_date, to_date, from_ts, to_ts):
    payments = [p for p in _paginate(company, creds, "/payments", {"from": from_ts, "to": to_ts}, PAGE)
                if p.get("status") in ("captured", "refunded", "authorized")]
    if not payments:
        return []

    settled = _fetch_recon(company, creds, from_date, to_date)

    def invoice_of(p):
        notes = p.get("notes")
        return (notes.get("sales_invoice") if isinstance(notes, dict) else None) or ""

    entries, donors = _erp_lookup({p["id"] for p in payments}, {invoice_of(p) for p in payments if invoice_of(p)})

    rows = []
    for p in payments:
        gross = _rupees(p.get("amount"))
        refunded = _rupees(p.get("amount_refunded"))
        fee = _rupees(p.get("fee"))           # fee INCLUDING GST (Razorpay doc)
        recon = settled.get(p["id"])

        if p["status"] == "authorized":
            state, net = AUTHORIZED, 0.0
        elif p["status"] == "refunded":
            state, net = REFUNDED, 0.0
        else:
            state = SETTLED if recon else PENDING
            net = round(gross - refunded - fee, 2)

        entry = entries.get(p["id"])
        if state == AUTHORIZED:
            erp = ERP_NA
        elif state == REFUNDED:
            erp = ERP_REFUND_GAP if entry else ERP_NA
        elif entry:
            erp = ERP_RECORDED if refunded == 0 else ERP_REFUND_GAP
        else:
            erp = ERP_MISSING

        invoice = invoice_of(p)
        rows.append({
            "date": _ts_date(p.get("created_at")),
            "company": company,
            "payment_id": p["id"],
            "sales_invoice": invoice or None,
            "donor": donors.get(invoice) or "",
            "method": p.get("method") or "",
            "status": state,
            "gross": gross,
            "refunded": refunded,
            "fee": fee,
            "net": net,
            "settlement_id": (recon or {}).get("settlement_id") or "",
            "utr": (recon or {}).get("settlement_utr") or "",
            "settled_on": _ts_date(recon["settled_at"]) if recon and recon.get("settled_at") else None,
            "payment_entry": entry,
            "erp_status": erp,
        })
    return rows


def _settlement_rows(company, creds, from_ts, to_ts):
    rows = []
    for s in _paginate(company, creds, "/settlements", {"from": from_ts, "to": to_ts}, PAGE):
        rows.append({
            "date": _ts_date(s.get("created_at")),
            "company": company,
            "settlement_id": s.get("id"),
            "utr": s.get("utr") or "",
            "status": (s.get("status") or "").title(),
            "amount": _rupees(s.get("amount")),
            "fees": _rupees(s.get("fees")),
            "tax": _rupees(s.get("tax")),
        })
    return rows


# --------------------------------------------------------------------------- #
# summaries
# --------------------------------------------------------------------------- #
def _matches_status(row, status):
    if not status or status == "All":
        return True
    if status == ERP_MISSING:
        return row["erp_status"] == ERP_MISSING
    return row["status"] == status


def _summary_rows(payment_rows):
    by_company = {}
    for r in payment_rows:
        s = by_company.setdefault(r["company"], {
            "company": r["company"], "captured_count": 0, "gross": 0.0, "refunded": 0.0, "fees": 0.0,
            "settled_count": 0, "settled_net": 0.0, "pending_count": 0, "pending_net": 0.0,
            "oldest_pending": None, "authorized_amount": 0.0, "missing_in_erp": 0,
        })
        if r["status"] == AUTHORIZED:
            s["authorized_amount"] = round(s["authorized_amount"] + r["gross"], 2)
            continue
        s["captured_count"] += 1
        s["gross"] = round(s["gross"] + r["gross"], 2)
        s["refunded"] = round(s["refunded"] + r["refunded"], 2)
        s["fees"] = round(s["fees"] + r["fee"], 2)
        if r["status"] == SETTLED:
            s["settled_count"] += 1
            s["settled_net"] = round(s["settled_net"] + r["net"], 2)
        elif r["status"] == PENDING:
            s["pending_count"] += 1
            s["pending_net"] = round(s["pending_net"] + r["net"], 2)
            if s["oldest_pending"] is None or r["date"] < s["oldest_pending"]:
                s["oldest_pending"] = r["date"]
        if r["erp_status"] == ERP_MISSING:
            s["missing_in_erp"] += 1
    return sorted(by_company.values(), key=lambda s: s["company"])


def _payment_summary(payment_rows):
    rows = _summary_rows(payment_rows)
    total = lambda key: round(sum(r[key] for r in rows), 2)
    cards = [
        {"label": _("Settled to Bank (net)"), "value": total("settled_net"), "datatype": "Currency", "indicator": "green"},
        {"label": _("Pending Settlement (net)"), "value": total("pending_net"), "datatype": "Currency", "indicator": "orange"},
        {"label": _("Razorpay Fees (incl. GST)"), "value": total("fees"), "datatype": "Currency", "indicator": "grey"},
        {"label": _("Refunded"), "value": total("refunded"), "datatype": "Currency", "indicator": "grey"},
    ]
    missing = sum(r["missing_in_erp"] for r in rows)
    if missing:
        cards.append({"label": _("Captured but Missing in ERPNext"), "value": missing, "datatype": "Int", "indicator": "red"})
    authorized = total("authorized_amount")
    if authorized:
        cards.append({"label": _("Authorized, not captured"), "value": authorized, "datatype": "Currency", "indicator": "red"})
    return cards


def _settlement_summary(rows):
    processed = round(sum(r["amount"] for r in rows if r["status"] == "Processed"), 2)
    other = round(sum(r["amount"] for r in rows if r["status"] != "Processed"), 2)
    cards = [
        {"label": _("Settled to Bank"), "value": processed, "datatype": "Currency", "indicator": "green"},
        {"label": _("Settlements"), "value": len(rows), "datatype": "Int", "indicator": "blue"},
    ]
    if other:
        cards.append({"label": _("Created / Failed (not yet in bank)"), "value": other, "datatype": "Currency", "indicator": "orange"})
    return cards


# --------------------------------------------------------------------------- #
# columns
# --------------------------------------------------------------------------- #
def _summary_columns():
    return [
        {"label": _("Trust"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 230},
        {"label": _("Payments Captured"), "fieldname": "captured_count", "fieldtype": "Int", "width": 100},
        {"label": _("Gross Captured"), "fieldname": "gross", "fieldtype": "Currency", "width": 120},
        {"label": _("Refunded"), "fieldname": "refunded", "fieldtype": "Currency", "width": 100},
        {"label": _("Fees (incl. GST)"), "fieldname": "fees", "fieldtype": "Currency", "width": 110},
        {"label": _("Settled (count)"), "fieldname": "settled_count", "fieldtype": "Int", "width": 90},
        {"label": _("Settled to Bank (net)"), "fieldname": "settled_net", "fieldtype": "Currency", "width": 140},
        {"label": _("Pending (count)"), "fieldname": "pending_count", "fieldtype": "Int", "width": 90},
        {"label": _("Pending Settlement (net)"), "fieldname": "pending_net", "fieldtype": "Currency", "width": 150},
        {"label": _("Oldest Pending Since"), "fieldname": "oldest_pending", "fieldtype": "Date", "width": 120},
        {"label": _("Authorized, not captured"), "fieldname": "authorized_amount", "fieldtype": "Currency", "width": 140},
        {"label": _("Missing in ERPNext"), "fieldname": "missing_in_erp", "fieldtype": "Int", "width": 100},
    ]


def _payment_columns():
    return [
        {"label": _("Date"), "fieldname": "date", "fieldtype": "Date", "width": 95},
        {"label": _("Trust"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 200},
        {"label": _("Razorpay Payment ID"), "fieldname": "payment_id", "fieldtype": "Data", "width": 190},
        {"label": _("Receipt / Invoice"), "fieldname": "sales_invoice", "fieldtype": "Link", "options": "Sales Invoice", "width": 140},
        {"label": _("Donor"), "fieldname": "donor", "fieldtype": "Data", "width": 160},
        {"label": _("Method"), "fieldname": "method", "fieldtype": "Data", "width": 90},
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 160},
        {"label": _("Gross"), "fieldname": "gross", "fieldtype": "Currency", "width": 100},
        {"label": _("Refunded"), "fieldname": "refunded", "fieldtype": "Currency", "width": 90},
        {"label": _("Fee (incl. GST)"), "fieldname": "fee", "fieldtype": "Currency", "width": 100},
        {"label": _("Net"), "fieldname": "net", "fieldtype": "Currency", "width": 100},
        {"label": _("Settlement ID"), "fieldname": "settlement_id", "fieldtype": "Data", "width": 170},
        {"label": _("UTR"), "fieldname": "utr", "fieldtype": "Data", "width": 150},
        {"label": _("Settled On"), "fieldname": "settled_on", "fieldtype": "Date", "width": 100},
        {"label": _("Payment Entry"), "fieldname": "payment_entry", "fieldtype": "Link", "options": "Payment Entry", "width": 150},
        {"label": _("ERPNext"), "fieldname": "erp_status", "fieldtype": "Data", "width": 170},
    ]


def _settlement_columns():
    return [
        {"label": _("Settlement Date"), "fieldname": "date", "fieldtype": "Date", "width": 110},
        {"label": _("Trust"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 230},
        {"label": _("Settlement ID"), "fieldname": "settlement_id", "fieldtype": "Data", "width": 190},
        {"label": _("UTR (bank reference)"), "fieldname": "utr", "fieldtype": "Data", "width": 190},
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
        {"label": _("Amount to Bank"), "fieldname": "amount", "fieldtype": "Currency", "width": 130},
        {"label": _("Fees"), "fieldname": "fees", "fieldtype": "Currency", "width": 90},
        {"label": _("Tax"), "fieldname": "tax", "fieldtype": "Currency", "width": 90},
    ]

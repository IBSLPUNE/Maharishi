# Donor Wise Donation Report  (Script Report)
#
# Who donated how much between two dates - donor-wise, donor-wise per trust
# (Company), or invoice-by-invoice. Works from the Sales Invoices that the
# donation page creates, so "received" means money actually paid against the
# invoice (grand_total - outstanding_amount).
#
# Notes
#  * Abandoned online checkouts leave UNPAID invoices behind. They are hidden by
#    default; tick "Include Unpaid Invoices" to see them in a separate column.
#  * Aadhaar is shown masked (XXXX XXXX 1234). PAN is shown in full because
#    80G / Form 10BD reporting needs it.
#  * Credit notes (is_return) are excluded.

import frappe
from frappe import _
from frappe.utils import flt, getdate

CHUNK = 400  # max values per "IN (...)" query

GROUP_DONOR = "Donor"
GROUP_DONOR_TRUST = "Donor + Trust"
GROUP_INVOICE = "Invoice-wise"


def execute(filters=None):
    filters = frappe._dict(filters or {})
    _validate(filters)

    group_by = filters.get("group_by") or GROUP_DONOR
    include_unpaid = bool(int(filters.get("include_unpaid") or 0))

    invoices, notes = _get_invoices(filters, include_unpaid)
    addresses = _get_latest_addresses({i.customer for i in invoices})

    if group_by == GROUP_INVOICE:
        payments = _get_payment_info([i.name for i in invoices])
        data = _invoice_rows(invoices, payments, include_unpaid)
        columns = _invoice_columns(include_unpaid)
    else:
        data = _grouped_rows(invoices, addresses, group_by == GROUP_DONOR_TRUST, include_unpaid)
        columns = _grouped_columns(group_by == GROUP_DONOR_TRUST, include_unpaid)

    return columns, data, "<br>".join(notes) or None, None, _summary(invoices, include_unpaid)


# --------------------------------------------------------------------------- #
# filters / queries
# --------------------------------------------------------------------------- #
def _validate(filters):
    if not filters.get("from_date") or not filters.get("to_date"):
        frappe.throw(_("From Date and To Date are required"))
    if getdate(filters.from_date) > getdate(filters.to_date):
        frappe.throw(_("From Date cannot be after To Date"))
    if (filters.get("group_by") or GROUP_DONOR) not in (GROUP_DONOR, GROUP_DONOR_TRUST, GROUP_INVOICE):
        frappe.throw(_("Invalid Group By"))


def _chunks(seq, n=CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


SETUP_HINT = "run  bench --site <your-site> execute maharishi.patches.setup_donation_fields.execute"


def _get_invoices(filters, include_unpaid):
    """Returns (invoice rows, notes). Optional custom fields that do not exist yet
    are read as empty instead of crashing the report - and a note says so."""
    has = frappe.db.has_column
    notes = []

    # The one field we cannot work without: it is how donation invoices are recognised.
    if not has("Sales Invoice", "custom_donor_mobile"):
        frappe.throw(_("Sales Invoice field 'custom_donor_mobile' does not exist yet. Please {0} first.").format(SETUP_HINT))

    def si(column):
        return "si." + column if has("Sales Invoice", column) else "NULL"

    def cust(column):
        return "c." + column if has("Customer", column) else "NULL"

    if not has("Sales Invoice", "custom_in_kind_details"):
        notes.append(_("In-kind donations cannot be separated yet: the Sales Invoice field "
                       "'custom_in_kind_details' has not been created. Please {0}.").format(SETUP_HINT))

    conditions = [
        "si.docstatus = 1",
        "si.is_return = 0",
        # only invoices created by the donation page (it always stores the mobile)
        "IFNULL(si.custom_donor_mobile, '') != ''",
        "si.posting_date >= %s",
        "si.posting_date <= %s",
    ]
    params = [getdate(filters.from_date).isoformat(), getdate(filters.to_date).isoformat()]

    for field, column, doctype in (("company", "company", "Sales Invoice"), ("customer", "customer", "Sales Invoice"),
                                   ("purpose", "custom_donation_purpose", "Sales Invoice")):
        if filters.get(field):
            if not has(doctype, column):
                frappe.throw(_("Cannot filter by {0}: the Sales Invoice field '{1}' does not exist yet. Please {2}.")
                             .format(field, column, SETUP_HINT))
            conditions.append("si." + column + " = %s")
            params.append(filters.get(field))

    if not include_unpaid:
        conditions.append("si.outstanding_amount < si.grand_total")

    rows = frappe.db.sql(
        """
        SELECT si.name, si.posting_date, si.company, si.customer, si.customer_name,
               si.grand_total, si.outstanding_amount,
               {purpose} AS purpose,
               {in_kind} AS in_kind,
               si.custom_donor_mobile AS si_mobile,
               {si_pan} AS si_pan,
               {si_aadhaar} AS si_aadhaar,
               c.mobile_no AS c_mobile, c.email_id AS c_email,
               {c_pan} AS c_pan, {c_aadhaar} AS c_aadhaar
        FROM `tabSales Invoice` si
        LEFT JOIN `tabCustomer` c ON c.name = si.customer
        WHERE {where}
        ORDER BY si.posting_date ASC, si.name ASC
        """.format(
            purpose=si("custom_donation_purpose"), in_kind=si("custom_in_kind_details"),
            si_pan=si("custom_donor_pan"), si_aadhaar=si("custom_donor_aadhaar"),
            c_pan=cust("pan"), c_aadhaar=cust("custom_aadhaar"),
            where=" AND ".join(conditions),
        ),
        tuple(params),
        as_dict=True,
    )

    for r in rows:
        r.received = max(round(flt(r.grand_total) - flt(r.outstanding_amount), 2), 0.0)
        r.unpaid = max(round(flt(r.outstanding_amount), 2), 0.0)
    return rows, notes


def _get_latest_addresses(customers):
    """customer -> most recently modified Address linked to them."""
    latest = {}
    for chunk in _chunks(sorted(customers)):
        placeholders = ", ".join(["%s"] * len(chunk))
        rows = frappe.db.sql(
            """
            SELECT dl.link_name AS customer, a.address_line1, a.address_line2,
                   a.city, a.state, a.pincode
            FROM `tabAddress` a
            JOIN `tabDynamic Link` dl ON dl.parent = a.name AND dl.parenttype = 'Address'
            WHERE dl.link_doctype = 'Customer' AND dl.link_name IN ({ph})
            ORDER BY a.modified ASC
            """.format(ph=placeholders),
            tuple(chunk),
            as_dict=True,
        )
        for r in rows:          # ascending by modified -> the last one wins
            latest[r.customer] = r
    return latest


def _get_payment_info(invoice_names):
    """invoice -> dict(modes, reference, payment_entry) from submitted Payment Entries."""
    info = {}
    for chunk in _chunks(invoice_names):
        placeholders = ", ".join(["%s"] * len(chunk))
        rows = frappe.db.sql(
            """
            SELECT per.reference_name AS invoice, pe.name AS payment_entry,
                   pe.mode_of_payment, pe.reference_no
            FROM `tabPayment Entry Reference` per
            JOIN `tabPayment Entry` pe ON pe.name = per.parent
            WHERE per.reference_doctype = 'Sales Invoice' AND pe.docstatus = 1
              AND per.reference_name IN ({ph})
            ORDER BY pe.posting_date ASC, pe.name ASC
            """.format(ph=placeholders),
            tuple(chunk),
            as_dict=True,
        )
        for r in rows:
            d = info.setdefault(r.invoice, {"modes": [], "refs": [], "pes": []})
            if r.mode_of_payment and r.mode_of_payment not in d["modes"]:
                d["modes"].append(r.mode_of_payment)
            if r.reference_no and r.reference_no not in d["refs"]:
                d["refs"].append(r.reference_no)
            d["pes"].append(r.payment_entry)
    return info


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _mask_aadhaar(value):
    value = (value or "").strip()
    return ("XXXX XXXX " + value[-4:]) if len(value) == 12 and value.isdigit() else ""


def _first(*values):
    for v in values:
        if v:
            return v
    return ""


# --------------------------------------------------------------------------- #
# donor-wise (optionally per trust)
# --------------------------------------------------------------------------- #
def _grouped_rows(invoices, addresses, by_trust, include_unpaid):
    groups = {}
    for inv in invoices:                      # ordered oldest -> newest
        key = (inv.customer, inv.company) if by_trust else (inv.customer,)
        g = groups.get(key)
        if g is None:
            g = groups[key] = frappe._dict(
                customer=inv.customer, company=inv.company if by_trust else None,
                donor_name="", pan="", aadhaar="", mobile="", email="",
                donations=0, first_date=None, last_date=None,
                received=0.0, in_kind_value=0.0, unpaid=0.0,
            )
        # newest invoice wins for descriptive fields
        g.donor_name = _first(inv.customer_name, g.donor_name)
        g.pan = _first(inv.c_pan, inv.si_pan, g.pan)
        g.aadhaar = _first(_mask_aadhaar(inv.c_aadhaar), _mask_aadhaar(inv.si_aadhaar), g.aadhaar)
        g.mobile = _first(inv.c_mobile, inv.si_mobile, g.mobile)
        g.email = _first(inv.c_email, g.email)

        if inv.received > 0:
            g.donations += 1
            g.received = round(g.received + inv.received, 2)
            if inv.in_kind:
                g.in_kind_value = round(g.in_kind_value + inv.received, 2)
            g.first_date = inv.posting_date if g.first_date is None else min(g.first_date, inv.posting_date)
            g.last_date = inv.posting_date if g.last_date is None else max(g.last_date, inv.posting_date)
        if include_unpaid and inv.unpaid > 0 and inv.received == 0:
            g.unpaid = round(g.unpaid + inv.unpaid, 2)

    rows = []
    for g in groups.values():
        addr = addresses.get(g.customer) or {}
        g.address_line1 = addr.get("address_line1") or ""
        g.address_line2 = addr.get("address_line2") or ""
        g.city = addr.get("city") or ""
        g.state = addr.get("state") or ""
        g.pincode = addr.get("pincode") or ""
        rows.append(g)

    rows.sort(key=lambda r: (-r.received, (r.donor_name or "").lower(), r.company or ""))
    return rows


def _grouped_columns(by_trust, include_unpaid):
    cols = [
        {"label": _("Donor ID"), "fieldname": "customer", "fieldtype": "Link", "options": "Customer", "width": 130},
        {"label": _("Donor Name"), "fieldname": "donor_name", "fieldtype": "Data", "width": 170},
        {"label": _("PAN"), "fieldname": "pan", "fieldtype": "Data", "width": 110},
        {"label": _("Aadhaar"), "fieldname": "aadhaar", "fieldtype": "Data", "width": 120},
        {"label": _("Mobile"), "fieldname": "mobile", "fieldtype": "Data", "width": 110},
        {"label": _("Email"), "fieldname": "email", "fieldtype": "Data", "width": 190},
        {"label": _("Address Line 1"), "fieldname": "address_line1", "fieldtype": "Data", "width": 200},
        {"label": _("Address Line 2"), "fieldname": "address_line2", "fieldtype": "Data", "width": 150},
        {"label": _("City"), "fieldname": "city", "fieldtype": "Data", "width": 110},
        {"label": _("State"), "fieldname": "state", "fieldtype": "Data", "width": 120},
        {"label": _("Pincode"), "fieldname": "pincode", "fieldtype": "Data", "width": 80},
    ]
    if by_trust:
        cols.append({"label": _("Trust"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 200})
    cols += [
        {"label": _("Donations"), "fieldname": "donations", "fieldtype": "Int", "width": 90},
        {"label": _("First Donation"), "fieldname": "first_date", "fieldtype": "Date", "width": 100},
        {"label": _("Last Donation"), "fieldname": "last_date", "fieldtype": "Date", "width": 100},
        {"label": _("Received"), "fieldname": "received", "fieldtype": "Currency", "width": 120},
        {"label": _("of which In-Kind (value)"), "fieldname": "in_kind_value", "fieldtype": "Currency", "width": 140},
    ]
    if include_unpaid:
        cols.append({"label": _("Unpaid (not received)"), "fieldname": "unpaid", "fieldtype": "Currency", "width": 130})
    return cols


# --------------------------------------------------------------------------- #
# invoice-wise
# --------------------------------------------------------------------------- #
def _invoice_rows(invoices, payments, include_unpaid):
    rows = []
    for inv in invoices:
        pay = payments.get(inv.name) or {}
        rows.append(frappe._dict(
            posting_date=inv.posting_date, invoice=inv.name, company=inv.company,
            customer=inv.customer, donor_name=inv.customer_name,
            pan=_first(inv.c_pan, inv.si_pan),
            aadhaar=_first(_mask_aadhaar(inv.c_aadhaar), _mask_aadhaar(inv.si_aadhaar)),
            mobile=_first(inv.c_mobile, inv.si_mobile),
            purpose=inv.purpose or "",
            payment_mode=", ".join(pay.get("modes") or []),
            reference=", ".join(pay.get("refs") or []),
            in_kind=inv.in_kind or "",
            received=inv.received,
            unpaid=inv.unpaid if inv.received == 0 else 0.0,
        ))
    return rows


def _invoice_columns(include_unpaid):
    cols = [
        {"label": _("Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 95},
        {"label": _("Receipt / Invoice"), "fieldname": "invoice", "fieldtype": "Link", "options": "Sales Invoice", "width": 140},
        {"label": _("Trust"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 200},
        {"label": _("Donor ID"), "fieldname": "customer", "fieldtype": "Link", "options": "Customer", "width": 120},
        {"label": _("Donor Name"), "fieldname": "donor_name", "fieldtype": "Data", "width": 170},
        {"label": _("PAN"), "fieldname": "pan", "fieldtype": "Data", "width": 110},
        {"label": _("Aadhaar"), "fieldname": "aadhaar", "fieldtype": "Data", "width": 120},
        {"label": _("Mobile"), "fieldname": "mobile", "fieldtype": "Data", "width": 110},
        {"label": _("Purpose"), "fieldname": "purpose", "fieldtype": "Link", "options": "Donation Type", "width": 150},
        {"label": _("Payment Mode"), "fieldname": "payment_mode", "fieldtype": "Data", "width": 120},
        {"label": _("Reference / UTR / Cheque No."), "fieldname": "reference", "fieldtype": "Data", "width": 170},
        {"label": _("In-Kind Details"), "fieldname": "in_kind", "fieldtype": "Data", "width": 170},
        {"label": _("Received"), "fieldname": "received", "fieldtype": "Currency", "width": 110},
    ]
    if include_unpaid:
        cols.append({"label": _("Unpaid (not received)"), "fieldname": "unpaid", "fieldtype": "Currency", "width": 130})
    return cols


# --------------------------------------------------------------------------- #
# summary cards
# --------------------------------------------------------------------------- #
def _summary(invoices, include_unpaid):
    paid = [i for i in invoices if i.received > 0]
    cards = [
        {"label": _("Total Received"), "value": round(sum(i.received for i in paid), 2), "datatype": "Currency", "indicator": "green"},
        {"label": _("Donations"), "value": len(paid), "datatype": "Int", "indicator": "blue"},
        {"label": _("Donors"), "value": len({i.customer for i in paid}), "datatype": "Int", "indicator": "blue"},
        {"label": _("In-Kind (value)"), "value": round(sum(i.received for i in paid if i.in_kind), 2), "datatype": "Currency", "indicator": "orange"},
    ]
    if include_unpaid:
        unpaid = [i for i in invoices if i.received == 0 and i.unpaid > 0]
        cards.append({"label": _("Unpaid (not received)"), "value": round(sum(i.unpaid for i in unpaid), 2),
                      "datatype": "Currency", "indicator": "red"})
    return cards

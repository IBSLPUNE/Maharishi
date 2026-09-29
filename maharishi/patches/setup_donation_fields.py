import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    custom_fields = {
        "Company": [
            {
                "fieldname": "custom_lineage",
                "label": "Lineage",
                "fieldtype": "Data",
                "insert_after": "company_name",
            },
            {
                "fieldname": "company_description",
                "label": "Description (Donation Page)",
                "fieldtype": "Small Text",
                "insert_after": "custom_lineage",
            },
            {
                "fieldname": "custom_show_on_donation_page",
                "label": "Show On Donation Page",
                "fieldtype": "Check",
                "insert_after": "company_description",
                "default": "0",
            },
            {
                "fieldname": "custom_razorpay_section",
                "label": "Razorpay (Donation Gateway)",
                "fieldtype": "Section Break",
                "insert_after": "custom_show_on_donation_page",
                "collapsible": 1,
            },
            {
                "fieldname": "custom_razorpay_key_id",
                "label": "Razorpay Key ID",
                "fieldtype": "Data",
                "insert_after": "custom_razorpay_section",
            },
            {
                "fieldname": "custom_razorpay_key_secret",
                "label": "Razorpay Key Secret",
                "fieldtype": "Password",
                "insert_after": "custom_razorpay_key_id",
            },
            {
                "fieldname": "custom_razorpay_webhook_secret",
                "label": "Razorpay Webhook Secret",
                "fieldtype": "Password",
                "insert_after": "custom_razorpay_key_secret",
            },
            {
                "fieldname": "custom_print_format",
                "label": "Donation Receipt Print Format",
                "fieldtype": "Link",
                "options": "Print Format",
                "insert_after": "custom_razorpay_webhook_secret",
            },
        ],

        "Customer": [
            {
                "fieldname": "custom_aadhaar",
                "label": "Aadhaar Number",
                "fieldtype": "Data",
                "insert_after": "pan",
                "length": 12,
            },
        ],

        "Sales Invoice": [
            {
                "fieldname": "custom_donor_mobile",
                "label": "Donor Mobile",
                "fieldtype": "Data",
                "insert_after": "customer",
            },
            {
                "fieldname": "custom_donor_pan",
                "label": "Donor PAN",
                "fieldtype": "Data",
                "insert_after": "custom_donor_mobile",
            },
            {
                "fieldname": "custom_donor_aadhaar",
                "label": "Donor Aadhaar",
                "fieldtype": "Data",
                "insert_after": "custom_donor_pan",
            },
            {
                "fieldname": "custom_donation_purpose",
                "label": "Donation Purpose",
                "fieldtype": "Link",
                "options": "Donation Type",
                "insert_after": "custom_donor_aadhaar",
            },
            {
                "fieldname": "custom_easebuzz_txnid",
                "label": "Easebuzz Txn ID",
                "fieldtype": "Data",
                "insert_after": "custom_donation_purpose",
            },
            {
                "fieldname": "custom_razorpay_order_id",
                "label": "Razorpay Order ID",
                "fieldtype": "Data",
                "insert_after": "custom_easebuzz_txnid",
            },
        ],

        "Payment Entry": [
            {
                "fieldname": "custom_bank_name",
                "label": "Bank Name (Cheque)",
                "fieldtype": "Data",
                "insert_after": "reference_date",
            },
        ],
    }

    for doctype, fields in custom_fields.items():

        for field in fields:
            fieldname = field["fieldname"]

            # Check whether field already exists as a Custom Field
            if frappe.db.exists(
                "Custom Field",
                {
                    "dt": doctype,
                    "fieldname": fieldname,
                },
            ):
                print(
                    f"SKIPPED: {doctype}.{fieldname} already exists"
                )
                continue

            # Also check standard DocType fields (in case it's a core field)
            meta = frappe.get_meta(doctype)

            if meta.has_field(fieldname):
                print(
                    f"SKIPPED: {doctype}.{fieldname} already exists in DocType"
                )
                continue

            try:
                create_custom_fields(
                    {
                        doctype: [field]
                    },
                    update=True,
                )

                print(
                    f"CREATED: {doctype}.{fieldname}"
                )

            except Exception as e:
                # Never stop migration because of one field
                print(
                    f"SKIPPED/ERROR: {doctype}.{fieldname} -> {str(e)}"
                )
                frappe.db.rollback()

    frappe.db.commit()

    print(
        "Donation custom fields migration completed successfully."
    )

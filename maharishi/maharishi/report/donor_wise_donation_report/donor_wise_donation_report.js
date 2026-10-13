frappe.query_reports["Donor Wise Donation Report"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.defaults.get_user_default("year_start_date") || frappe.datetime.add_months(frappe.datetime.get_today(), -12),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "company",
			label: __("Trust (leave empty for all trusts)"),
			fieldtype: "Link",
			options: "Company",
		},
		{
			fieldname: "group_by",
			label: __("Group By"),
			fieldtype: "Select",
			options: "Donor\nDonor + Trust\nInvoice-wise",
			default: "Donor",
			reqd: 1,
		},
		{
			fieldname: "customer",
			label: __("Donor"),
			fieldtype: "Link",
			options: "Customer",
		},
		{
			fieldname: "purpose",
			label: __("Purpose"),
			fieldtype: "Link",
			options: "Donation Type",
		},
		{
			fieldname: "include_unpaid",
			label: __("Include Unpaid Invoices"),
			fieldtype: "Check",
			default: 0,
		},
	],
};


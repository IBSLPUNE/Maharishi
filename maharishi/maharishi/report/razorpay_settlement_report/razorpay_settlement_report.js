frappe.query_reports["Razorpay Settlement Report"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
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
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: "Summary (Trust-wise)\nPayment-wise\nSettlement-wise",
			default: "Summary (Trust-wise)",
			reqd: 1,
		},
		{
			fieldname: "status",
			label: __("Status (Payment-wise view)"),
			fieldtype: "Select",
			options: "All\nSettled\nPending Settlement\nRefunded\nAuthorized (not captured)\nMissing in ERPNext",
			default: "All",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;

		const color = { "Settled": "green", "Pending Settlement": "orange", "Refunded": "gray",
		                "Authorized (not captured)": "red", "Recorded": "green",
		                "Missing in ERPNext": "red", "Refunded - not in ERPNext": "red",
		                "Processed": "green", "Created": "orange", "Failed": "red" };
		if (["status", "erp_status"].includes(column.fieldname) && color[data[column.fieldname]]) {
			value = `<span style="color:var(--${color[data[column.fieldname]]}-600, ${color[data[column.fieldname]]}); font-weight:600">${value}</span>`;
		}
		return value;
	},
};

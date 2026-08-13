frappe.ui.form.on("Project", {
    refresh(frm) {
        const can_manage_accounting =
            frappe.session.user === "Administrator" ||
            frappe.user_roles.includes("System Manager") ||
            frappe.user_roles.includes("Accounts Manager");

        if (frm.is_new() || !can_manage_accounting) {
            return;
        }

        frm.add_custom_button(
            __("Reassign Sales Order Advances"),
            () => open_advance_reassignment_dialog(frm),
            __("Accounting")
        );
    },
});

function parse_document_names(value) {
    return (value || "")
        .split(/[\n,]+/)
        .map((name) => name.trim())
        .filter(Boolean);
}

function open_advance_reassignment_dialog(frm) {
    const dialog = new frappe.ui.Dialog({
        title: __("Reassign Sales Order Advances"),
        fields: [
            {
                fieldname: "target_order",
                fieldtype: "Link",
                options: "Sales Order",
                label: __("Target Sales Order"),
                reqd: 1,
                get_query: () => ({
                    filters: {
                        project: frm.doc.name,
                        docstatus: 1,
                    },
                }),
            },
            {
                fieldname: "payment_entries",
                fieldtype: "Small Text",
                label: __("Payment Entries"),
                description: __("Enter Payment Entry IDs separated by commas or new lines."),
                reqd: 1,
            },
            {
                fieldname: "source_orders",
                fieldtype: "Small Text",
                label: __("Source Sales Orders"),
                description: __("Enter obsolete Sales Order IDs separated by commas or new lines."),
                reqd: 1,
            },
            {
                fieldname: "cancel_source_orders",
                fieldtype: "Check",
                label: __("Cancel source Sales Orders after reassignment"),
                default: 1,
            },
        ],
        primary_action_label: __("Review"),
        primary_action(values) {
            const payment_entries = parse_document_names(values.payment_entries);
            const source_orders = parse_document_names(values.source_orders);
            if (!payment_entries.length || !source_orders.length) {
                frappe.msgprint(__("Payment Entries and source Sales Orders are required."));
                return;
            }

            dialog.get_primary_btn().prop("disabled", true);
            frappe.call({
                method: "payments.api.accounting_cleanup.reassign_sales_order_advances",
                args: {
                    project: frm.doc.name,
                    target_order: values.target_order,
                    payment_entries,
                    source_orders,
                    dry_run: 1,
                    cancel_source_orders: values.cancel_source_orders ? 1 : 0,
                },
            }).then((response) => {
                const plan = response.message;
                const payment_lines = plan.payments
                    .map((row) => `${frappe.utils.escape_html(row.payment_entry)}: ${format_currency(row.amount)}`)
                    .join("<br>");
                const source_lines = plan.source_orders
                    .map((name) => frappe.utils.escape_html(name))
                    .join(", ");

                frappe.confirm(
                    __(
                        "Move {0} to {1}?<br><br>{2}<br><br>Source orders: {3}<br><br>This cancels and amends the Payment Entries so the ledger audit trail is preserved.",
                        [
                            format_currency(plan.total_to_move),
                            frappe.utils.escape_html(plan.target_order),
                            payment_lines,
                            source_lines,
                        ]
                    ),
                    () => execute_advance_reassignment(frm, dialog, values, payment_entries, source_orders),
                    () => dialog.get_primary_btn().prop("disabled", false)
                );
            }).catch(() => {
                dialog.get_primary_btn().prop("disabled", false);
            });
        },
    });

    dialog.show();
}

function execute_advance_reassignment(frm, dialog, values, payment_entries, source_orders) {
    frappe.call({
        method: "payments.api.accounting_cleanup.reassign_sales_order_advances",
        freeze: true,
        freeze_message: __("Reassigning advances..."),
        args: {
            project: frm.doc.name,
            target_order: values.target_order,
            payment_entries,
            source_orders,
            dry_run: 0,
            cancel_source_orders: values.cancel_source_orders ? 1 : 0,
        },
    }).then((response) => {
        const result = response.message;
        const replacements = result.amended_payments
            .map((row) =>
                `${frappe.utils.escape_html(row.cancelled)} → ${frappe.utils.escape_html(row.replacement)} (${format_currency(row.amount)})`
            )
            .join("<br>");

        dialog.hide();
        frappe.msgprint({
            title: __("Advances reassigned"),
            indicator: "green",
            message: __(
                "Payment Entry replacements:<br>{0}<br><br>Cancelled source orders: {1}",
                [
                    replacements,
                    result.cancelled_orders.map((name) => frappe.utils.escape_html(name)).join(", ") || __("None"),
                ]
            ),
        });
        frm.reload_doc();
    }).catch(() => {
        dialog.get_primary_btn().prop("disabled", false);
    });
}

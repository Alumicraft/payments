// Copyright (c) 2026, Your Company and contributors
// For license information, please see license.txt

frappe.ui.form.on('Payment Request', {
    refresh: function (frm) {
        // Add Stripe-related buttons when invoice exists
        if (frm.doc.stripe_invoice_url) {
            // Add Stripe button group
            frm.add_custom_button(__('Open Invoice'), function () {
                window.open(frm.doc.stripe_invoice_url, '_blank');
            }, __('Stripe'));

            frm.add_custom_button(__('Copy Link'), function () {
                frappe.utils.copy_to_clipboard(frm.doc.stripe_invoice_url);
                frappe.show_alert({
                    message: __('Invoice link copied to clipboard!'),
                    indicator: 'green'
                });
            }, __('Stripe'));

            // Admin-only: View in Stripe Dashboard
            if (frappe.user.has_role('System Manager') && frm.doc.stripe_invoice_id) {
                frm.add_custom_button(__('View in Dashboard'), function () {
                    const dashboard_url = `https://dashboard.stripe.com/invoices/${frm.doc.stripe_invoice_id}`;
                    window.open(dashboard_url, '_blank');
                }, __('Stripe'));
            }

            // Regenerate button (only if pending)
            if (!frm.doc.stripe_payment_status || frm.doc.stripe_payment_status === 'Pending') {
                frm.add_custom_button(__('Regenerate Invoice'), function () {
                    frappe.confirm(
                        __('This will void the current invoice and create a new one. The customer will receive a new payment link. Continue?'),
                        function () {
                            frappe.call({
                                method: 'payments.utils.regenerate_stripe_invoice',
                                args: {
                                    payment_request_name: frm.doc.name
                                },
                                freeze: true,
                                freeze_message: __('Regenerating invoice...'),
                                callback: function (r) {
                                    if (r.message && r.message.success) {
                                        frappe.show_alert({
                                            message: __('Invoice regenerated successfully!'),
                                            indicator: 'green'
                                        });
                                        frm.reload_doc();
                                    }
                                }
                            });
                        }
                    );
                }, __('Stripe'));
            }

            // Refresh status from Stripe
            frm.add_custom_button(__('Refresh Status'), function () {
                frappe.call({
                    method: 'payments.utils.get_stripe_invoice_status',
                    args: {
                        payment_request_name: frm.doc.name
                    },
                    freeze: true,
                    freeze_message: __('Checking status...'),
                    callback: function (r) {
                        if (r.message) {
                            const status = r.message;
                            if (status.error) {
                                frappe.msgprint({
                                    title: __('Error'),
                                    indicator: 'red',
                                    message: status.error
                                });
                            } else {
                                frappe.msgprint({
                                    title: __('Stripe Invoice Status'),
                                    indicator: status.paid ? 'green' : 'orange',
                                    message: `
                                        <strong>Status:</strong> ${status.status}<br>
                                        <strong>Amount Due:</strong> ${format_currency(status.amount_due, status.currency)}<br>
                                        <strong>Amount Paid:</strong> ${format_currency(status.amount_paid, status.currency)}
                                    `
                                });
                                frm.reload_doc();
                            }
                        }
                    }
                });
            }, __('Stripe'));
        }

        // Update payment status indicator
        update_status_indicator(frm);


    },

    allow_card_payment: function (frm) {
        calculate_card_fees(frm);

        // Warn if invoice already exists
        if (frm.doc.stripe_invoice_id && frm.doc.stripe_payment_status === 'Pending') {
            frappe.msgprint({
                title: __('Invoice Update Required'),
                indicator: 'orange',
                message: __('You have changed the card payment option. Click "Regenerate Invoice" in the Stripe menu to update the invoice with the new payment options.')
            });
        }
    },

    grand_total: function (frm) {
        if (frm.doc.allow_card_payment) {
            calculate_card_fees(frm);
        }
    }
});


/**
 * Calculate card processing fees (3% flat rate)
 */
function calculate_card_fees(frm) {
    if (frm.doc.allow_card_payment && frm.doc.grand_total) {
        const card_fee = flt(frm.doc.grand_total * 0.03, 2);
        const total_with_fee = flt(frm.doc.grand_total + card_fee, 2);

        frm.set_value('card_processing_fee', card_fee);
        frm.set_value('total_with_card_fee', total_with_fee);


    } else {
        frm.set_value('card_processing_fee', 0);
        frm.set_value('total_with_card_fee', 0);
    }
}





/**
 * Update page indicator based on payment status
 */
function update_status_indicator(frm) {
    const status_config = {
        'Pending': 'orange',
        'Paid': 'green',
        'Failed': 'red',
        'Voided': 'grey',
        'Action Required': 'yellow'
    };

    const status = frm.doc.stripe_payment_status;

    if (status && status_config[status]) {
        frm.page.set_indicator(status, status_config[status]);
    }
}

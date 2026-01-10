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
                                method: 'stripe_integration.stripe_integration.regenerate_stripe_invoice',
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
                    method: 'stripe_integration.stripe_integration.get_stripe_invoice_status',
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

        // Show pricing info if card payment is enabled
        if (frm.doc.allow_card_payment && frm.doc.grand_total) {
            show_pricing_info(frm);
        }
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

        show_pricing_info(frm);
    } else {
        frm.set_value('card_processing_fee', 0);
        frm.set_value('total_with_card_fee', 0);
    }
}


/**
 * Show pricing comparison info in dashboard
 */
function show_pricing_info(frm) {
    // Clear existing comments
    frm.dashboard.clear_comment();

    if (frm.doc.allow_card_payment && frm.doc.grand_total) {
        const base_amount = frm.doc.grand_total;
        const card_fee = frm.doc.card_processing_fee || (base_amount * 0.03);
        const total_with_fee = frm.doc.total_with_card_fee || (base_amount + card_fee);

        const currency = frm.doc.currency || 'USD';

        frm.dashboard.add_comment(
            `<div style="padding: 10px; background: #f0f4f7; border-radius: 5px; margin-bottom: 10px;">
                <strong>💳 Payment Options for Customer:</strong><br>
                <div style="margin-top: 5px;">
                    <span style="color: #28a745;">✓ ACH Direct Debit:</span> 
                    <strong>${format_currency(base_amount, currency)}</strong> 
                    <span style="color: #666; font-size: 11px;">(Recommended - saves 3%!)</span>
                </div>
                <div style="margin-top: 3px;">
                    <span style="color: #17a2b8;">✓ Card Payment:</span> 
                    <strong>${format_currency(total_with_fee, currency)}</strong>
                    <span style="color: #666; font-size: 11px;">(includes ${format_currency(card_fee, currency)} processing fee)</span>
                </div>
            </div>`,
            'blue',
            true
        );
    }
}


/**
 * Update page indicator based on payment status
 */
function update_status_indicator(frm) {
    const status_config = {
        'Pending': { color: 'orange', icon: '⏳' },
        'Paid': { color: 'green', icon: '✓' },
        'Failed': { color: 'red', icon: '✗' },
        'Voided': { color: 'grey', icon: '○' },
        'Action Required': { color: 'yellow', icon: '!' }
    };

    const status = frm.doc.stripe_payment_status;

    if (status && status_config[status]) {
        const config = status_config[status];
        frm.page.set_indicator(
            `${config.icon} Stripe: ${status}`,
            config.color
        );
    }
}

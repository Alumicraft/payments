// Copyright (c) 2024, Your Company and contributors
// For license information, please see license.txt

/**
 * Loan Form Customization for ACH Autopay
 *
 * Adds "Setup Auto-Pay" or "Manage Auto-Pay" button to submitted Loans
 * based on whether an ACH Authorization exists.
 */

frappe.ui.form.on('Loan', {
    refresh: function(frm) {
        // Only show for submitted loans
        if (frm.doc.docstatus !== 1) {
            return;
        }

        // Check if ACH is enabled and add appropriate button
        frappe.call({
            method: 'payments.payments.doctype.ach_settings.ach_settings.is_ach_enabled',
            callback: function(r) {
                if (r.message) {
                    check_and_add_autopay_button(frm);
                }
            }
        });
    }
});


function check_and_add_autopay_button(frm) {
    // Check for existing authorization
    frappe.call({
        method: 'payments.api.achq_integration.get_authorization_status',
        args: {
            loan: frm.doc.name
        },
        callback: function(r) {
            if (r.message && r.message.has_authorization) {
                add_manage_autopay_button(frm, r.message);
            } else {
                add_setup_autopay_button(frm);
            }
        }
    });
}


function add_setup_autopay_button(frm) {
    frm.add_custom_button(__('Setup Auto-Pay'), function() {
        show_setup_dialog(frm);
    }, __('Actions'));
}


function add_manage_autopay_button(frm, auth_info) {
    frm.add_custom_button(__('Manage Auto-Pay'), function() {
        show_manage_dialog(frm, auth_info);
    }, __('Actions'));
}


function show_setup_dialog(frm) {
    const dialog = new frappe.ui.Dialog({
        title: __('Setup Auto-Pay'),
        fields: [
            {
                fieldname: 'info_html',
                fieldtype: 'HTML',
                options: `
                    <div class="alert alert-info">
                        <p><strong>Automatic Payment Authorization</strong></p>
                        <p>By providing your bank account information, you authorize automatic withdrawals for your loan payments.</p>
                    </div>
                `
            },
            {
                fieldname: 'routing_number',
                fieldtype: 'Data',
                label: __('Routing Number'),
                reqd: 1,
                description: __('9-digit bank routing number')
            },
            {
                fieldname: 'account_number',
                fieldtype: 'Data',
                label: __('Account Number'),
                reqd: 1
            },
            {
                fieldname: 'confirm_account_number',
                fieldtype: 'Data',
                label: __('Confirm Account Number'),
                reqd: 1
            },
            {
                fieldname: 'account_type',
                fieldtype: 'Select',
                label: __('Account Type'),
                options: 'Checking\nSavings',
                default: 'Checking',
                reqd: 1
            },
            {
                fieldname: 'consent_section',
                fieldtype: 'Section Break'
            },
            {
                fieldname: 'consent',
                fieldtype: 'Check',
                label: __('I authorize automatic withdrawals from my bank account for loan payments. I understand that I can cancel this authorization at any time.'),
                reqd: 1
            }
        ],
        primary_action_label: __('Setup Auto-Pay'),
        primary_action: function(values) {
            // Validate inputs
            if (!validate_setup_inputs(values)) {
                return;
            }

            dialog.disable_primary_action();

            frappe.call({
                method: 'payments.api.achq_integration.setup_bank_account',
                args: {
                    customer: frm.doc.applicant,
                    loan: frm.doc.name,
                    routing_number: values.routing_number,
                    account_number: values.account_number,
                    account_type: values.account_type
                },
                callback: function(r) {
                    if (r.message && r.message.success) {
                        dialog.hide();
                        frappe.show_alert({
                            message: __('Auto-Pay has been successfully set up for account ending in {0}',
                                [r.message.account_last4]),
                            indicator: 'green'
                        });
                        frm.reload_doc();
                    }
                },
                error: function() {
                    dialog.enable_primary_action();
                }
            });
        }
    });

    dialog.show();
}


function validate_setup_inputs(values) {
    // Validate routing number (9 digits)
    const routing = values.routing_number.replace(/\D/g, '');
    if (routing.length !== 9) {
        frappe.msgprint(__('Routing number must be exactly 9 digits'));
        return false;
    }

    // Validate account numbers match
    if (values.account_number !== values.confirm_account_number) {
        frappe.msgprint(__('Account numbers do not match'));
        return false;
    }

    // Validate account number (numeric, 4-17 digits)
    const account = values.account_number.replace(/\D/g, '');
    if (account.length < 4 || account.length > 17) {
        frappe.msgprint(__('Account number must be between 4 and 17 digits'));
        return false;
    }

    // Validate consent
    if (!values.consent) {
        frappe.msgprint(__('Please accept the authorization consent'));
        return false;
    }

    return true;
}


function show_manage_dialog(frm, auth_info) {
    const status_color = auth_info.status === 'Active' ? 'green' : 'orange';

    const dialog = new frappe.ui.Dialog({
        title: __('Manage Auto-Pay'),
        fields: [
            {
                fieldname: 'info_html',
                fieldtype: 'HTML',
                options: `
                    <div class="autopay-info" style="margin-bottom: 15px;">
                        <p><strong>Bank Account:</strong> ${auth_info.bank_name || 'Bank'} ending in ${auth_info.account_last4}</p>
                        <p><strong>Account Type:</strong> ${auth_info.account_type}</p>
                        <p><strong>Status:</strong> <span class="indicator-pill ${status_color}">${auth_info.status}</span></p>
                    </div>
                `
            }
        ],
        primary_action_label: auth_info.status === 'Active' ? __('Pause Auto-Pay') : __('Resume Auto-Pay'),
        primary_action: function() {
            if (auth_info.status === 'Active') {
                pause_authorization(auth_info.authorization_name, dialog, frm);
            } else {
                resume_authorization(auth_info.authorization_name, dialog, frm);
            }
        },
        secondary_action_label: __('Cancel Auto-Pay'),
        secondary_action: function() {
            confirm_revoke_authorization(auth_info.authorization_name, dialog, frm);
        }
    });

    dialog.show();
}


function pause_authorization(auth_name, dialog, frm) {
    frappe.prompt([
        {
            fieldname: 'reason',
            fieldtype: 'Small Text',
            label: __('Reason for Pausing (optional)')
        }
    ], function(values) {
        frappe.call({
            method: 'payments.api.achq_integration.pause_authorization',
            args: {
                authorization_name: auth_name,
                reason: values.reason
            },
            callback: function(r) {
                if (r.message && r.message.success) {
                    dialog.hide();
                    frappe.show_alert({
                        message: __('Auto-Pay has been paused'),
                        indicator: 'orange'
                    });
                    frm.reload_doc();
                }
            }
        });
    }, __('Pause Auto-Pay'), __('Pause'));
}


function resume_authorization(auth_name, dialog, frm) {
    frappe.call({
        method: 'payments.api.achq_integration.resume_authorization',
        args: {
            authorization_name: auth_name
        },
        callback: function(r) {
            if (r.message && r.message.success) {
                dialog.hide();
                frappe.show_alert({
                    message: __('Auto-Pay has been resumed'),
                    indicator: 'green'
                });
                frm.reload_doc();
            }
        }
    });
}


function confirm_revoke_authorization(auth_name, dialog, frm) {
    frappe.confirm(
        __('Are you sure you want to cancel Auto-Pay? Any scheduled payments will be cancelled.'),
        function() {
            frappe.prompt([
                {
                    fieldname: 'reason',
                    fieldtype: 'Small Text',
                    label: __('Reason for Cancellation (optional)')
                }
            ], function(values) {
                frappe.call({
                    method: 'payments.api.achq_integration.revoke_authorization',
                    args: {
                        authorization_name: auth_name,
                        reason: values.reason
                    },
                    callback: function(r) {
                        if (r.message && r.message.success) {
                            dialog.hide();
                            frappe.show_alert({
                                message: __('Auto-Pay has been cancelled'),
                                indicator: 'red'
                            });
                            frm.reload_doc();
                        }
                    }
                });
            }, __('Cancel Auto-Pay'), __('Confirm'));
        }
    );
}

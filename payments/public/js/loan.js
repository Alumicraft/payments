// Copyright (c) 2024, Your Company and contributors
// For license information, please see license.txt

/**
 * Loan Form Customization for ACH Autopay
 *
 * Features:
 * - Multiple bank accounts per customer
 * - Plaid Link as primary connection method
 * - Manual entry as fallback
 * - Per-loan account override
 * - Default account management
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
                    add_manage_autopay_button(frm);
                }
            }
        });
    }
});


function add_manage_autopay_button(frm) {
    frm.add_custom_button(__('Manage Auto-Pay'), function() {
        show_autopay_manager(frm);
    }, __('Actions'));
}


function show_autopay_manager(frm) {
    // Get customer's bank accounts and loan's current payment account
    Promise.all([
        frappe.call({
            method: 'payments.api.achq_integration.get_customer_accounts',
            args: { customer: frm.doc.applicant }
        }),
        frappe.call({
            method: 'payments.api.achq_integration.get_loan_account_info',
            args: { loan: frm.doc.name }
        }),
        frappe.call({
            method: 'payments.api.achq_integration.is_plaid_available'
        })
    ]).then(function([accounts_r, loan_r, plaid_r]) {
        const accounts = accounts_r.message.accounts || [];
        const loan_account = loan_r.message;
        const plaid_available = plaid_r.message.available;

        show_autopay_dialog(frm, accounts, loan_account, plaid_available);
    });
}


function show_autopay_dialog(frm, accounts, loan_account, plaid_available) {
    let accounts_html = '';

    if (accounts.length === 0) {
        accounts_html = `
            <div class="text-muted text-center" style="padding: 20px;">
                <p>No bank accounts set up yet.</p>
                <p>Add a bank account to enable automatic payments.</p>
            </div>
        `;
    } else {
        accounts_html = '<div class="bank-accounts-list">';
        for (const acc of accounts) {
            const is_current = loan_account.has_account && loan_account.authorization_name === acc.name;
            const is_default = acc.is_default;
            const status_color = acc.status === 'Active' ? 'green' : 'orange';

            let badges = '';
            if (is_default) badges += '<span class="badge badge-primary">Default</span> ';
            if (is_current) badges += '<span class="badge badge-success">Using for this loan</span>';
            if (acc.token_source === 'Plaid') badges += ' <span class="badge badge-info">Plaid</span>';

            accounts_html += `
                <div class="bank-account-item" data-auth="${acc.name}" style="border: 1px solid #d1d8dd; border-radius: 4px; padding: 12px; margin-bottom: 10px;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                            <strong>${acc.bank_name || 'Bank Account'}</strong> ending in ${acc.bank_account_last4}
                            <br>
                            <small class="text-muted">${acc.account_type} &bull; <span class="indicator-pill ${status_color}">${acc.status}</span></small>
                            <br>
                            ${badges}
                        </div>
                        <div class="account-actions">
                            ${!is_current ? `<button class="btn btn-xs btn-primary use-for-loan" data-auth="${acc.name}">Use for This Loan</button>` : ''}
                            ${!is_default && acc.status === 'Active' ? `<button class="btn btn-xs btn-default set-default" data-auth="${acc.name}">Set as Default</button>` : ''}
                            <button class="btn btn-xs btn-danger remove-account" data-auth="${acc.name}">Remove</button>
                        </div>
                    </div>
                </div>
            `;
        }
        accounts_html += '</div>';
    }

    // Show resolution info
    let resolution_html = '';
    if (loan_account.has_account) {
        const resolution_text = loan_account.resolution === 'loan_override'
            ? 'This loan uses a specific account override'
            : 'This loan uses the customer\'s default account';
        resolution_html = `
            <div class="alert alert-info" style="margin-bottom: 15px;">
                <strong>Current Payment Account:</strong> ${loan_account.bank_name || 'Bank'} ending in ${loan_account.account_last4}
                <br><small>${resolution_text}</small>
                ${loan_account.resolution === 'loan_override' ? '<br><button class="btn btn-xs btn-default clear-override" style="margin-top: 5px;">Use Default Instead</button>' : ''}
            </div>
        `;
    } else {
        resolution_html = `
            <div class="alert alert-warning" style="margin-bottom: 15px;">
                <strong>No Payment Account:</strong> Add a bank account to enable automatic payments.
            </div>
        `;
    }

    const dialog = new frappe.ui.Dialog({
        title: __('Manage Auto-Pay'),
        size: 'large',
        fields: [
            {
                fieldname: 'accounts_html',
                fieldtype: 'HTML',
                options: resolution_html + accounts_html
            },
            {
                fieldname: 'add_section',
                fieldtype: 'Section Break',
                label: 'Add Bank Account'
            },
            {
                fieldname: 'add_buttons_html',
                fieldtype: 'HTML',
                options: `
                    <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                        ${plaid_available ?
                            `<button class="btn btn-primary btn-plaid">
                                <i class="fa fa-university"></i> Connect with Plaid
                            </button>` : ''}
                        <button class="btn btn-default btn-manual">
                            <i class="fa fa-keyboard-o"></i> Enter Manually
                        </button>
                    </div>
                `
            }
        ]
    });

    dialog.show();

    // Bind button events
    dialog.$wrapper.find('.btn-plaid').on('click', function() {
        dialog.hide();
        start_plaid_link(frm);
    });

    dialog.$wrapper.find('.btn-manual').on('click', function() {
        dialog.hide();
        show_manual_entry_dialog(frm);
    });

    dialog.$wrapper.find('.use-for-loan').on('click', function() {
        const auth_name = $(this).data('auth');
        set_loan_account(frm, auth_name, dialog);
    });

    dialog.$wrapper.find('.set-default').on('click', function() {
        const auth_name = $(this).data('auth');
        set_default_account(auth_name, dialog, frm);
    });

    dialog.$wrapper.find('.remove-account').on('click', function() {
        const auth_name = $(this).data('auth');
        confirm_remove_account(auth_name, dialog, frm);
    });

    dialog.$wrapper.find('.clear-override').on('click', function() {
        clear_loan_override(frm, dialog);
    });
}


function start_plaid_link(frm) {
    // Get Plaid Link token
    frappe.call({
        method: 'payments.api.achq_integration.get_plaid_link_token',
        args: { customer: frm.doc.applicant },
        callback: function(r) {
            if (r.message && r.message.success) {
                open_plaid_link(frm, r.message.link_token);
            }
        }
    });
}


function open_plaid_link(frm, link_token) {
    // Load Plaid Link script if not already loaded
    if (typeof Plaid === 'undefined') {
        const script = document.createElement('script');
        script.src = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js';
        script.onload = function() {
            create_plaid_handler(frm, link_token);
        };
        document.head.appendChild(script);
    } else {
        create_plaid_handler(frm, link_token);
    }
}


function create_plaid_handler(frm, link_token) {
    const handler = Plaid.create({
        token: link_token,
        onSuccess: function(public_token, metadata) {
            // User selected an account
            const account = metadata.accounts[0];
            process_plaid_success(frm, public_token, account.id);
        },
        onExit: function(err, metadata) {
            if (err) {
                // User had an error - offer manual entry
                frappe.confirm(
                    __("Couldn't connect to your bank. Would you like to enter your account details manually?"),
                    function() {
                        show_manual_entry_dialog(frm);
                    }
                );
            }
            // If no error, user just closed - reopen manager
            show_autopay_manager(frm);
        },
        onEvent: function(eventName, metadata) {
            // Optional: log events for debugging
        }
    });

    handler.open();
}


function process_plaid_success(frm, public_token, account_id) {
    frappe.call({
        method: 'payments.api.achq_integration.process_plaid_callback',
        args: {
            public_token: public_token,
            account_id: account_id,
            customer: frm.doc.applicant,
            is_default: true
        },
        freeze: true,
        freeze_message: __('Connecting bank account...'),
        callback: function(r) {
            if (r.message && r.message.success) {
                frappe.show_alert({
                    message: __('Bank account connected: {0} ending in {1}',
                        [r.message.bank_name, r.message.account_last4]),
                    indicator: 'green'
                });
                frm.reload_doc();
            }
        }
    });
}


function show_manual_entry_dialog(frm) {
    const dialog = new frappe.ui.Dialog({
        title: __('Add Bank Account Manually'),
        fields: [
            {
                fieldname: 'info_html',
                fieldtype: 'HTML',
                options: `
                    <div class="alert alert-info">
                        <p>Enter your bank account details below. Your account will be verified before activation.</p>
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
                fieldname: 'is_default',
                fieldtype: 'Check',
                label: __('Set as default payment account'),
                default: 1
            },
            {
                fieldname: 'consent_section',
                fieldtype: 'Section Break'
            },
            {
                fieldname: 'consent',
                fieldtype: 'Check',
                label: __('I authorize automatic withdrawals from my bank account for loan payments.'),
                reqd: 1
            }
        ],
        primary_action_label: __('Add Account'),
        primary_action: function(values) {
            if (!validate_manual_entry(values)) {
                return;
            }

            dialog.disable_primary_action();

            frappe.call({
                method: 'payments.api.achq_integration.setup_bank_account',
                args: {
                    customer: frm.doc.applicant,
                    routing_number: values.routing_number,
                    account_number: values.account_number,
                    account_type: values.account_type,
                    is_default: values.is_default
                },
                callback: function(r) {
                    if (r.message && r.message.success) {
                        dialog.hide();
                        frappe.show_alert({
                            message: __('Bank account added: ending in {0}', [r.message.account_last4]),
                            indicator: 'green'
                        });
                        frm.reload_doc();
                    }
                },
                error: function() {
                    dialog.enable_primary_action();
                }
            });
        },
        secondary_action_label: __('Back'),
        secondary_action: function() {
            dialog.hide();
            show_autopay_manager(frm);
        }
    });

    dialog.show();
}


function validate_manual_entry(values) {
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


function set_loan_account(frm, auth_name, dialog) {
    frappe.call({
        method: 'payments.api.achq_integration.set_loan_account',
        args: {
            loan: frm.doc.name,
            authorization_name: auth_name
        },
        callback: function(r) {
            if (r.message && r.message.success) {
                dialog.hide();
                frappe.show_alert({
                    message: __('Payment account updated for this loan'),
                    indicator: 'green'
                });
                frm.reload_doc();
            }
        }
    });
}


function clear_loan_override(frm, dialog) {
    frappe.call({
        method: 'payments.api.achq_integration.set_loan_account',
        args: {
            loan: frm.doc.name,
            authorization_name: ''  // Empty to clear override
        },
        callback: function(r) {
            if (r.message && r.message.success) {
                dialog.hide();
                frappe.show_alert({
                    message: __('This loan will now use the default payment account'),
                    indicator: 'green'
                });
                frm.reload_doc();
            }
        }
    });
}


function set_default_account(auth_name, dialog, frm) {
    frappe.call({
        method: 'payments.api.achq_integration.set_default_account',
        args: {
            authorization_name: auth_name
        },
        callback: function(r) {
            if (r.message && r.message.success) {
                dialog.hide();
                frappe.show_alert({
                    message: __('Default payment account updated'),
                    indicator: 'green'
                });
                frm.reload_doc();
            }
        }
    });
}


function confirm_remove_account(auth_name, dialog, frm) {
    frappe.confirm(
        __('Are you sure you want to remove this bank account? Any scheduled payments using this account will be affected.'),
        function() {
            frappe.prompt([
                {
                    fieldname: 'reason',
                    fieldtype: 'Small Text',
                    label: __('Reason (optional)')
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
                                message: __('Bank account removed'),
                                indicator: 'red'
                            });
                            frm.reload_doc();
                        }
                    }
                });
            }, __('Remove Bank Account'), __('Remove'));
        }
    );
}

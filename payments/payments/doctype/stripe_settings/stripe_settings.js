frappe.ui.form.on('Stripe Settings', {
    refresh(frm) {
        frm.add_custom_button('Show Webhook Secret', () => {
            frappe.call({
                method: 'frappe.client.get_password',
                args: {
                    doctype: 'Stripe Settings',
                    name: 'Stripe Settings',
                    fieldname: 'webhook_secret'
                },
                callback(r) {
                    if (r.message) {
                        frappe.msgprint({
                            title: 'Webhook Signing Secret',
                            message: `<code style="word-break: break-all;">${r.message}</code>`,
                            indicator: 'blue'
                        });
                    } else {
                        frappe.msgprint('No webhook secret configured.');
                    }
                }
            });
        });
    }
});

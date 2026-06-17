import frappe

from payments.utils import (
    PAYMENT_REQUEST_AMOUNT_TOLERANCE,
    get_payment_status_after_external_payment,
    should_sync_external_payment_status,
    to_currency_float,
)


def execute():
    """Repair Payment Requests that already have matching submitted Payment Entries."""
    sync_exact_reference_payment_requests()
    sync_project_amount_payment_requests()


def sync_exact_reference_payment_requests():
    """Repair requests whose exact reference already has a submitted Payment Entry."""
    payment_requests = frappe.db.sql(
        """
        select
            pr.name,
            pr.status,
            pr.stripe_invoice_id,
            pr.stripe_payment_status
        from `tabPayment Request` pr
        where pr.docstatus = 1
          and ifnull(pr.reference_doctype, '') != ''
          and ifnull(pr.reference_name, '') != ''
          and exists (
              select 1
              from `tabPayment Entry Reference` per
              inner join `tabPayment Entry` pe on pe.name = per.parent
              where pe.docstatus = 1
                and per.reference_doctype = pr.reference_doctype
                and per.reference_name = pr.reference_name
                and ifnull(per.allocated_amount, 0) > 0
          )
          and (
              ifnull(pr.status, '') != 'Paid'
              or ifnull(pr.stripe_payment_status, '') in ('Pending', 'N/A', '')
          )
        """,
        as_dict=True,
    )

    for payment_request in payment_requests:
        mark_payment_request_paid(payment_request)


def sync_project_amount_payment_requests():
    """
    Repair Sales Order requests after a later Sales Invoice was paid.

    ERPNext Payment Entry rows usually reference the Sales Invoice, while older
    Payment Requests may still point at the Sales Order. Project plus exact
    amount is the durable bridge between those records.
    """
    payment_requests = frappe.db.sql(
        """
        select
            pr.name,
            pr.status,
            pr.stripe_invoice_id,
            pr.stripe_payment_status,
            pr.grand_total,
            coalesce(nullif(pr.project, ''), nullif(si.project, ''), nullif(so.project, '')) as project
        from `tabPayment Request` pr
        left join `tabSales Invoice` si
            on pr.reference_doctype = 'Sales Invoice'
            and pr.reference_name = si.name
        left join `tabSales Order` so
            on pr.reference_doctype = 'Sales Order'
            and pr.reference_name = so.name
        where pr.docstatus = 1
          and ifnull(pr.grand_total, 0) > 0
          and coalesce(nullif(pr.project, ''), nullif(si.project, ''), nullif(so.project, '')) is not null
          and (
              ifnull(pr.status, '') != 'Paid'
              or ifnull(pr.stripe_payment_status, '') in ('Pending', 'N/A', '')
          )
        """,
        as_dict=True,
    )

    synced_payment_requests = set()
    for payment_request in payment_requests:
        if payment_request.name in synced_payment_requests:
            continue

        if not has_project_amount_payment_entry(
            payment_request.project,
            to_currency_float(payment_request.grand_total),
        ):
            continue

        if mark_payment_request_paid(payment_request):
            synced_payment_requests.add(payment_request.name)


def has_project_amount_payment_entry(project, amount):
    if not project or amount <= 0:
        return False

    payment_entries = frappe.db.sql(
        """
        select pe.name
        from `tabPayment Entry` pe
        left join `tabPayment Entry Reference` per
            on per.parent = pe.name
        left join `tabSales Invoice` si
            on per.reference_doctype = 'Sales Invoice'
            and per.reference_name = si.name
        left join `tabSales Order` so
            on per.reference_doctype = 'Sales Order'
            and per.reference_name = so.name
        where pe.docstatus = 1
          and (
              nullif(pe.project, '') = %(project)s
              or nullif(si.project, '') = %(project)s
              or nullif(so.project, '') = %(project)s
              or ifnull(pe.reference_no, '') like %(project_like)s
              or ifnull(pe.remarks, '') like %(project_like)s
          )
          and (
              abs(ifnull(per.allocated_amount, 0) - %(amount)s) <= %(tolerance)s
              or (
                  ifnull(per.allocated_amount, 0) = 0
                  and abs(ifnull(pe.paid_amount, 0) - %(amount)s) <= %(tolerance)s
              )
          )
        limit 1
        """,
        {
            "project": project,
            "project_like": f"%{project}%",
            "amount": amount,
            "tolerance": PAYMENT_REQUEST_AMOUNT_TOLERANCE,
        },
        as_dict=True,
    )

    return bool(payment_entries)


def mark_payment_request_paid(payment_request):
    if (
        payment_request.status == "Paid"
        and not should_sync_external_payment_status(payment_request.stripe_payment_status)
    ):
        return False

    values = {"status": "Paid"}
    if should_sync_external_payment_status(payment_request.stripe_payment_status):
        stripe_payment_status = get_payment_status_after_external_payment(
            payment_request.stripe_invoice_id
        )
        values["stripe_payment_status"] = stripe_payment_status

    frappe.db.set_value(
        "Payment Request",
        payment_request.name,
        values,
        update_modified=False,
    )
    return True

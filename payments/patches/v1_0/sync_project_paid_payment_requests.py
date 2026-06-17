from payments.patches.v1_0.sync_manually_paid_payment_requests import (
    sync_project_amount_payment_requests,
)


def execute():
    """Repair stale Payment Requests paid through a same-project invoice."""
    sync_project_amount_payment_requests()

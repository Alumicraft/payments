from payments.patches.v1_0.sync_manually_paid_payment_requests import (
    sync_unallocated_party_payment_requests,
)


def execute():
    """Repair stale Payment Requests paid by unallocated customer advances."""
    sync_unallocated_party_payment_requests()

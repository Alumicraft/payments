from payments.patches.v1_0.reconcile_stripe_payment_requests import (
    create_missing_entries_for_paid_requests,
)
from payments.utils import get_stripe_settings


def execute():
    settings = get_stripe_settings()
    if settings:
        create_missing_entries_for_paid_requests(settings)

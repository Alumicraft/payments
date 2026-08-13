from erpnext.projects.doctype.project.project_dashboard import get_data as get_standard_data


def get_data(data=None):
    """Preserve ERPNext's Project dashboard and expose linked Payment Requests."""
    data = data if data is not None else get_standard_data()
    transactions = data.setdefault("transactions", [])

    sales_group = next(
        (group for group in transactions if group.get("label") == "Sales"),
        None,
    )
    if sales_group is None:
        sales_group = {"label": "Sales", "items": []}
        transactions.append(sales_group)

    items = sales_group.setdefault("items", [])
    if "Payment Request" not in items:
        items.append("Payment Request")

    return data

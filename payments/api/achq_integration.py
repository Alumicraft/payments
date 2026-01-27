# Copyright (c) 2024, Your Company and contributors
# For license information, please see license.txt

"""
ACHQ Integration Module

Provides client for ACHQ API operations and webhook handling.
Based on ACHQ API documentation at developers.achq.com
"""

import json
import frappe
from frappe import _
from frappe.utils import now_datetime, getdate, today
import requests


# ACHQ Status to internal status mapping
ACHQ_STATUS_MAP = {
    "Scheduled": "Scheduled",
    "InProcess": "Processing",
    "Cleared": "Success",
    "Settled": "Success",
    "Returned": "Returned",
    "Returned-NSF": "Returned",
    "Returned-Other": "Returned",
    "ChargedBack": "Returned",
    "Cancelled": "Cancelled",
    "Rejected": "Failed",
}


class ACHQClient:
    """Client for ACHQ API operations using Direct Merchant mode."""

    BASE_URL = "https://www.speedchex.com/datalinks/transact.aspx"

    def __init__(self):
        self.settings = frappe.get_single("ACH Settings")
        self._validate_settings()

    def _validate_settings(self):
        """Validate that required settings are configured."""
        if not self.settings.enable_ach_autopay:
            frappe.throw(_("ACH Autopay is not enabled"))

    def _get_auth_params(self):
        """Get authentication parameters for Direct Merchant mode."""
        params = {
            "MerchantID": self.settings.achq_merchant_id,
            "Merchant_GateID": self.settings.achq_merchant_gate_id,
            "Merchant_GateKey": self.settings.get_password("achq_merchant_gate_key"),
        }

        # Add TestMode for sandbox
        if self.settings.achq_environment == "Sandbox":
            params["TestMode"] = "On"

        return params

    def _make_request(self, command, params):
        """Make a request to ACHQ API."""
        data = self._get_auth_params()
        data["Command"] = command
        data["CommandVersion"] = "2.0"
        data["ResponseType"] = "JSON"
        data.update(params)

        try:
            response = requests.post(self.BASE_URL, data=data, timeout=30)
            response.raise_for_status()
            return self._parse_response(response.text)
        except requests.RequestException as e:
            frappe.log_error(
                f"ACHQ API request failed: {str(e)}",
                "ACHQ Integration"
            )
            return {"success": False, "error_message": str(e)}

    def _parse_response(self, response_text):
        """Parse ACHQ JSON response."""
        if not response_text:
            return {"success": False, "error_message": "Empty response"}

        try:
            result = json.loads(response_text)
        except json.JSONDecodeError as e:
            frappe.log_error(
                f"ACHQ response JSON parse error: {str(e)}\nResponse: {response_text[:500]}",
                "ACHQ Integration"
            )
            return {"success": False, "error_message": f"Invalid JSON response: {str(e)}"}

        # Check for success based on CommandStatus
        command_status = result.get("CommandStatus", "").lower()
        response_code = result.get("ResponseCode", "")

        if command_status == "approved" or response_code == "000":
            result["success"] = True
        else:
            result["success"] = False
            result["error_message"] = result.get("Description",
                result.get("ErrorInformation", {}).get("Message", "Unknown error")
                if isinstance(result.get("ErrorInformation"), dict)
                else result.get("ErrorInformation", "Unknown error")
            )
            result["error_code"] = response_code

        return result

    def tokenize_and_verify(self, routing_number, account_number, account_type, customer_name, check_type=None):
        """
        Create a token and verify the bank account.

        Args:
            routing_number: 9-digit routing number
            account_number: Bank account number
            account_type: 'Checking' or 'Savings'
            customer_name: Customer's name for the account
            check_type: 'Personal' or 'Business' (defaults to settings)

        Returns:
            dict with success, token, bank_name, verify_status
        """
        params = {
            "RoutingNumber": routing_number,
            "AccountNumber": account_number,
            "AccountType": account_type,
            "CheckType": check_type or self.settings.default_check_type or "Personal",
        }

        # Add Express Verify if enabled
        if self.settings.use_express_verify:
            params["Run_ExpressVerify"] = "Yes"

        result = self._make_request("ECheck.CreateACHQToken", params)

        if result.get("success"):
            # Handle nested ExpressVerify response
            express_verify = result.get("ExpressVerify", {})
            if isinstance(express_verify, dict):
                verify_status = express_verify.get("Status", "UNK")
                verify_code = express_verify.get("Code")
                verify_desc = express_verify.get("Description")
            else:
                verify_status = "UNK"
                verify_code = None
                verify_desc = None

            return {
                "success": True,
                "token": result.get("ACHQToken"),
                "bank_name": result.get("BankName", ""),
                "verify_status": verify_status,
                "verify_code": verify_code,
                "verify_description": verify_desc,
                "routing_last4": routing_number[-4:] if len(routing_number) >= 4 else routing_number,
                "account_last4": account_number[-4:] if len(account_number) >= 4 else account_number,
                "transact_reference_id": result.get("TransAct_ReferenceID"),
            }

        return result

    def create_payment(self, amount, token, customer_name, description, txn_id, customer_ip=None):
        """
        Create a payment using a tokenized account.

        Args:
            amount: Payment amount
            token: ACHQ token from tokenize_and_verify
            customer_name: Customer's name
            description: Payment description
            txn_id: Internal transaction ID for tracking
            customer_ip: Customer's IP address (optional)

        Returns:
            dict with success, transaction_id, status
        """
        params = {
            "Amount": f"{float(amount):.2f}",
            "AccountToken": token,
            "PaymentDirection": "FromCustomer",
            "SECCode": self.settings.default_sec_code,
            "Billing_CustomerName": customer_name,
            "Description": description[:50] if description else "",
            "Merchant_ReferenceID": txn_id,
        }

        if customer_ip:
            params["Customer_IPAddress"] = customer_ip

        result = self._make_request("ECheck.ProcessPayment", params)

        if result.get("success"):
            return {
                "success": True,
                "transaction_id": result.get("TransactionID"),
                "status": result.get("PaymentStatus", "Scheduled"),
                "transact_reference_id": result.get("TransAct_ReferenceID"),
            }

        return result

    def get_status_by_date(self, tracking_date):
        """
        Get all payment status updates for a given date.

        This is the correct way to poll for status updates in ACHQ.
        Returns all transactions that had status changes on the specified date.

        Args:
            tracking_date: Date to query (date object or string YYYY-MM-DD)

        Returns:
            dict with success, transactions list
        """
        if isinstance(tracking_date, str):
            tracking_date = getdate(tracking_date)

        # ACHQ expects MMDDYYYY format
        date_str = tracking_date.strftime("%m%d%Y")

        params = {
            "TrackingDate": date_str,
        }

        result = self._make_request("ECheckReports.StatusTrackingQuery", params)

        if result.get("success"):
            # Parse transactions from response
            transactions = result.get("Transactions", [])
            if not isinstance(transactions, list):
                transactions = [transactions] if transactions else []

            return {
                "success": True,
                "transactions": transactions,
                "tracking_date": tracking_date,
            }

        return result

    def cancel_payment(self, transaction_id):
        """
        Cancel a scheduled payment.

        Args:
            transaction_id: ACHQ transaction ID

        Returns:
            dict with success
        """
        params = {
            "TransactionID": transaction_id,
        }

        result = self._make_request("ECheck.CancelPayment", params)
        return result


@frappe.whitelist(allow_guest=True)
def achq_webhook():
    """
    Handle ACHQ webhook callbacks.

    URL: /api/method/payments.api.achq_integration.achq_webhook

    Handles events:
    - Payment Cleared
    - Payment Returned
    - Payment Failed
    """
    try:
        # Get webhook data
        data = frappe.local.form_dict

        # Log incoming webhook for debugging
        frappe.logger().info(f"ACHQ Webhook received: {data}")

        # Extract key fields
        transaction_id = data.get("TransactionID")
        merchant_ref_id = data.get("Merchant_ReferenceID")
        payment_status = data.get("PaymentStatus", "").lower()
        return_code = data.get("ReturnCode")
        return_description = data.get("ReturnDescription")

        if not transaction_id and not merchant_ref_id:
            frappe.log_error("ACHQ Webhook: No transaction ID provided", "ACHQ Webhook")
            return {"status": "error", "message": "No transaction ID"}

        # Find the ACH Transaction
        txn = None
        if merchant_ref_id:
            # Merchant_ReferenceID is our internal transaction name
            if frappe.db.exists("ACH Transaction", merchant_ref_id):
                txn = frappe.get_doc("ACH Transaction", merchant_ref_id)

        if not txn and transaction_id:
            # Look up by ACHQ transaction ID
            txn_name = frappe.db.get_value(
                "ACH Transaction",
                {"achq_transaction_id": transaction_id},
                "name"
            )
            if txn_name:
                txn = frappe.get_doc("ACH Transaction", txn_name)

        if not txn:
            frappe.log_error(
                f"ACHQ Webhook: Transaction not found - ACHQ ID: {transaction_id}, Merchant Ref: {merchant_ref_id}",
                "ACHQ Webhook"
            )
            return {"status": "error", "message": "Transaction not found"}

        # Update transaction based on status
        txn.achq_status = payment_status

        if payment_status in ("cleared", "settled", "success"):
            txn.mark_success(achq_status=payment_status)
        elif payment_status in ("returned", "returned-nsf", "returned-other", "chargedback"):
            txn.mark_failed(
                failure_code=return_code,
                failure_reason=return_description,
                return_code=return_code
            )
        elif payment_status in ("failed", "declined", "rejected"):
            txn.mark_failed(
                failure_code=return_code or "FAILED",
                failure_reason=return_description or "Payment failed"
            )
        elif payment_status == "cancelled":
            if txn.status not in ("Cancelled", "Success"):
                txn.status = "Cancelled"
                txn.save()

        frappe.db.commit()

        return {"status": "success"}

    except Exception as e:
        frappe.log_error(
            f"ACHQ Webhook error: {str(e)}\nData: {frappe.local.form_dict}",
            "ACHQ Webhook Error"
        )
        return {"status": "error", "message": str(e)}


@frappe.whitelist()
def setup_bank_account(customer, loan, routing_number, account_number, account_type, check_type=None):
    """
    Set up a bank account for ACH autopay.

    Called from Loan.js when customer sets up auto-pay.

    Args:
        customer: Customer name
        loan: Loan name
        routing_number: 9-digit routing number
        account_number: Bank account number
        account_type: 'Checking' or 'Savings'
        check_type: 'Personal' or 'Business' (optional)

    Returns:
        dict with success, bank_name, account_last4, authorization_name
    """
    # Validate inputs
    if not customer or not loan or not routing_number or not account_number:
        frappe.throw(_("All fields are required"))

    # Validate routing number format (9 digits)
    routing_number = routing_number.strip()
    if not routing_number.isdigit() or len(routing_number) != 9:
        frappe.throw(_("Routing number must be exactly 9 digits"))

    # Validate account number (numeric, reasonable length)
    account_number = account_number.strip()
    if not account_number.isdigit():
        frappe.throw(_("Account number must contain only digits"))
    if len(account_number) < 4 or len(account_number) > 17:
        frappe.throw(_("Account number must be between 4 and 17 digits"))

    # Check loan belongs to customer
    loan_customer = frappe.db.get_value("Loan", loan, "applicant")
    if loan_customer != customer:
        frappe.throw(_("Loan does not belong to this customer"))

    # Check no existing active authorization
    existing = frappe.db.exists(
        "ACH Authorization",
        {"loan": loan, "status": "Active"}
    )
    if existing:
        frappe.throw(_("An active ACH Authorization already exists for this loan. Please revoke it first."))

    # Get customer name for ACHQ
    customer_name = frappe.db.get_value("Customer", customer, "customer_name")

    # Create token and verify with ACHQ
    client = ACHQClient()
    result = client.tokenize_and_verify(
        routing_number=routing_number,
        account_number=account_number,
        account_type=account_type,
        customer_name=customer_name,
        check_type=check_type
    )

    if not result.get("success"):
        frappe.throw(_("Bank account verification failed: {0}").format(
            result.get("error_message", "Unknown error")
        ))

    verify_status = result.get("verify_status", "UNK")

    # Check verification status
    if verify_status == "NEG":
        frappe.throw(_("Bank account verification failed. This account cannot be used for autopay."))

    # Check if UNK is allowed
    settings = frappe.get_single("ACH Settings")
    if verify_status == "UNK" and not settings.allow_unknown_accounts:
        frappe.throw(_("Bank account could not be verified. Please contact support."))

    # Create ACH Authorization
    auth = frappe.new_doc("ACH Authorization")
    auth.customer = customer
    auth.loan = loan
    auth.status = "Active"
    auth.bank_name = result.get("bank_name", "")
    auth.account_type = account_type
    auth.bank_account_last4 = result.get("account_last4", "")
    auth.routing_number_last4 = result.get("routing_last4", "")
    auth.achq_token = result.get("token")
    auth.verification_status = verify_status
    auth.consent_captured = 1
    auth.authorization_ip = frappe.local.request_ip if hasattr(frappe.local, 'request_ip') else ""
    auth.authorization_date = now_datetime()
    auth.sec_code = settings.default_sec_code
    auth.insert()

    frappe.db.commit()

    return {
        "success": True,
        "bank_name": result.get("bank_name", ""),
        "account_last4": result.get("account_last4", ""),
        "authorization_name": auth.name,
        "verification_status": verify_status,
        "message": "Bank account successfully linked for autopay"
    }


@frappe.whitelist()
def get_authorization_status(loan):
    """
    Get the current ACH authorization status for a loan.

    Args:
        loan: Loan name

    Returns:
        dict with has_authorization, status, bank_name, account_last4, authorization_name
    """
    auth = frappe.db.get_value(
        "ACH Authorization",
        {"loan": loan, "status": ["in", ["Active", "Paused"]]},
        ["name", "status", "bank_name", "bank_account_last4", "account_type"],
        as_dict=True
    )

    if auth:
        return {
            "has_authorization": True,
            "authorization_name": auth.name,
            "status": auth.status,
            "bank_name": auth.bank_name,
            "account_last4": auth.bank_account_last4,
            "account_type": auth.account_type,
        }

    return {
        "has_authorization": False,
    }


@frappe.whitelist()
def pause_authorization(authorization_name, reason=None):
    """Pause an ACH authorization."""
    auth = frappe.get_doc("ACH Authorization", authorization_name)
    auth.pause(reason)
    return {"success": True, "message": "Authorization paused"}


@frappe.whitelist()
def resume_authorization(authorization_name):
    """Resume a paused ACH authorization."""
    auth = frappe.get_doc("ACH Authorization", authorization_name)
    auth.resume()
    return {"success": True, "message": "Authorization resumed"}


@frappe.whitelist()
def revoke_authorization(authorization_name, reason=None):
    """Revoke an ACH authorization."""
    auth = frappe.get_doc("ACH Authorization", authorization_name)
    auth.revoke(reason)
    return {"success": True, "message": "Authorization revoked"}

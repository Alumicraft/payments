# Copyright (c) 2024, Your Company and contributors
# For license information, please see license.txt

"""
ACHQ Integration Module

Provides client for ACHQ API operations and webhook handling.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, cint
import requests
import hashlib
import hmac


# ACHQ Status to internal status mapping
ACHQ_STATUS_MAP = {
    "Scheduled": "Scheduled",
    "InProcess": "Processing",
    "Cleared": "Success",
    "Returned-NSF": "Returned",
    "Returned-Other": "Returned",
    "ChargedBack": "Returned",
    "Cancelled": "Cancelled",
}


class ACHQClient:
    """Client for ACHQ API operations."""

    SANDBOX_URL = "https://www.speedchex.com/datalinks/transact.aspx"
    PRODUCTION_URL = "https://www.speedchex.com/datalinks/transact.aspx"

    def __init__(self):
        self.settings = frappe.get_single("ACH Settings")
        self._validate_settings()

    def _validate_settings(self):
        """Validate that required settings are configured."""
        if not self.settings.enable_ach_autopay:
            frappe.throw(_("ACH Autopay is not enabled"))

    @property
    def base_url(self):
        """Get the appropriate base URL based on environment."""
        if self.settings.achq_environment == "Production":
            return self.PRODUCTION_URL
        return self.SANDBOX_URL

    def _get_auth_params(self):
        """Get common authentication parameters."""
        return {
            "ProviderID": self.settings.achq_provider_id,
            "Provider_GateID": self.settings.achq_provider_gate_id,
            "Provider_GateKey": self.settings.get_password("achq_provider_gate_key"),
            "MerchantID": self.settings.achq_merchant_id,
            "Merchant_GateID": self.settings.achq_merchant_gate_id,
            "Merchant_GateKey": self.settings.get_password("achq_merchant_gate_key"),
        }

    def _make_request(self, command, params):
        """Make a request to ACHQ API."""
        data = self._get_auth_params()
        data["Command"] = command
        data.update(params)

        try:
            response = requests.post(self.base_url, data=data, timeout=30)
            response.raise_for_status()
            return self._parse_response(response.text)
        except requests.RequestException as e:
            frappe.log_error(
                f"ACHQ API request failed: {str(e)}",
                "ACHQ Integration"
            )
            return {"success": False, "error_message": str(e)}

    def _parse_response(self, response_text):
        """Parse ACHQ response (pipe-delimited format)."""
        # ACHQ returns responses in format: Field1=Value1|Field2=Value2|...
        result = {}
        if not response_text:
            return {"success": False, "error_message": "Empty response"}

        pairs = response_text.strip().split("|")
        for pair in pairs:
            if "=" in pair:
                key, value = pair.split("=", 1)
                result[key.strip()] = value.strip()

        # Check for success
        status = result.get("Status", "").upper()
        if status in ("APPROVED", "SUCCESS", "OK"):
            result["success"] = True
        else:
            result["success"] = False
            result["error_message"] = result.get("Message", result.get("ErrorMessage", "Unknown error"))
            result["error_code"] = result.get("ErrorCode", result.get("Code", ""))

        return result

    def tokenize_and_verify(self, routing_number, account_number, account_type, customer_name):
        """
        Create a token and verify the bank account.

        Args:
            routing_number: 9-digit routing number
            account_number: Bank account number
            account_type: 'Checking' or 'Savings'
            customer_name: Customer's name for the account

        Returns:
            dict with success, token, bank_name, verify_status
        """
        params = {
            "RoutingNumber": routing_number,
            "AccountNumber": account_number,
            "AccountType": account_type,
            "Billing_CustomerName": customer_name,
            "Create_ACHQToken": "Yes",
        }

        # Add Express Verify if enabled
        if self.settings.use_express_verify:
            params["Run_ExpressVerify"] = "Yes"

        result = self._make_request("ECheck.CreateToken", params)

        if result.get("success"):
            return {
                "success": True,
                "token": result.get("ACHQToken"),
                "bank_name": result.get("BankName", ""),
                "verify_status": result.get("ExpressVerify_Status", "UNK"),
                "routing_last4": routing_number[-4:] if len(routing_number) >= 4 else routing_number,
                "account_last4": account_number[-4:] if len(account_number) >= 4 else account_number,
            }

        return result

    def create_payment(self, amount, token, customer_name, description, txn_id):
        """
        Create a payment using a tokenized account.

        Args:
            amount: Payment amount
            token: ACHQ token from tokenize_and_verify
            customer_name: Customer's name
            description: Payment description
            txn_id: Internal transaction ID for tracking

        Returns:
            dict with success, transaction_id, status
        """
        params = {
            "Amount": f"{float(amount):.2f}",
            "AccountToken": token,
            "PaymentDirection": "FromCustomer",
            "SECCode": self.settings.default_sec_code,
            "Billing_CustomerName": customer_name,
            "Description": description[:50] if description else "",  # ACHQ limit
            "Provider_TransactionID": txn_id,
        }

        result = self._make_request("ECheck.ProcessPayment", params)

        if result.get("success"):
            return {
                "success": True,
                "transaction_id": result.get("TransactionID"),
                "status": result.get("PaymentStatus", "Scheduled"),
            }

        return result

    def get_payment_status(self, transaction_id):
        """
        Get the status of a payment.

        Args:
            transaction_id: ACHQ transaction ID

        Returns:
            dict with success, status, return_code, return_description
        """
        params = {
            "TransactionID": transaction_id,
        }

        result = self._make_request("ECheck.GetPaymentStatus", params)

        if result.get("success"):
            return {
                "success": True,
                "status": result.get("PaymentStatus"),
                "return_code": result.get("ReturnCode"),
                "return_description": result.get("ReturnDescription"),
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

        # Log incoming webhook
        frappe.log_error(
            f"ACHQ Webhook received: {data}",
            "ACHQ Webhook Debug"
        )

        # Extract key fields
        transaction_id = data.get("TransactionID")
        provider_txn_id = data.get("Provider_TransactionID")
        payment_status = data.get("PaymentStatus", "").lower()
        return_code = data.get("ReturnCode")
        return_description = data.get("ReturnDescription")

        if not transaction_id and not provider_txn_id:
            frappe.log_error("ACHQ Webhook: No transaction ID provided", "ACHQ Webhook")
            return {"status": "error", "message": "No transaction ID"}

        # Find the ACH Transaction
        txn = None
        if provider_txn_id:
            # Provider_TransactionID is our internal transaction name
            if frappe.db.exists("ACH Transaction", provider_txn_id):
                txn = frappe.get_doc("ACH Transaction", provider_txn_id)

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
                f"ACHQ Webhook: Transaction not found - ACHQ ID: {transaction_id}, Provider ID: {provider_txn_id}",
                "ACHQ Webhook"
            )
            return {"status": "error", "message": "Transaction not found"}

        # Update transaction based on status
        txn.achq_status = payment_status

        if payment_status in ("cleared", "success"):
            txn.mark_success(achq_status=payment_status)
        elif payment_status in ("returned", "returned-nsf", "returned-other", "chargedback"):
            txn.mark_failed(
                failure_code=return_code,
                failure_reason=return_description,
                return_code=return_code
            )
        elif payment_status in ("failed", "declined"):
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
        # Return success to prevent retries for processing errors
        return {"status": "error", "message": str(e)}


@frappe.whitelist()
def setup_bank_account(customer, loan, routing_number, account_number, account_type):
    """
    Set up a bank account for ACH autopay.

    Called from Loan.js when customer sets up auto-pay.

    Args:
        customer: Customer name
        loan: Loan name
        routing_number: 9-digit routing number
        account_number: Bank account number
        account_type: 'Checking' or 'Savings'

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
        customer_name=customer_name
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

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

    def create_payment(self, amount, token, customer_name, description, txn_id, customer_ip=None, token_source=None):
        """
        Create a payment using a tokenized account.

        Args:
            amount: Payment amount
            token: ACHQ token or Plaid processor_token
            customer_name: Customer's name
            description: Payment description
            txn_id: Internal transaction ID for tracking
            customer_ip: Customer's IP address (optional)
            token_source: 'Manual' or 'Plaid' - determines how token is processed

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

        # For Plaid tokens, tell ACHQ the token source
        if token_source == "Plaid":
            params["TokenSource"] = "Plaid"

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

    URL: /api/method/payments_for_lending.api.achq_integration.achq_webhook

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
def setup_bank_account(customer, routing_number, account_number, account_type, is_default=True, check_type=None):
    """
    Set up a bank account for ACH autopay using manual entry.

    Creates an ACH Authorization linked to the customer (not loan-specific).

    Args:
        customer: Customer name
        routing_number: 9-digit routing number
        account_number: Bank account number
        account_type: 'Checking' or 'Savings'
        is_default: Set as default payment account (default True)
        check_type: 'Personal' or 'Business' (optional)

    Returns:
        dict with success, bank_name, account_last4, authorization_name
    """
    # Validate inputs
    if not customer or not routing_number or not account_number:
        frappe.throw(_("Customer, routing number, and account number are required"))

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

    # Get customer name for ACHQ
    customer_name = frappe.db.get_value("Customer", customer, "customer_name")
    if not customer_name:
        frappe.throw(_("Customer not found"))

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

    # Convert is_default to boolean
    is_default = is_default in [True, 1, "1", "true", "True"]

    # Create ACH Authorization (customer-level, not loan-specific)
    auth = frappe.new_doc("ACH Authorization")
    auth.customer = customer
    auth.is_default = 1 if is_default else 0
    auth.status = "Active"
    auth.token_source = "Manual"
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
        "is_default": auth.is_default,
        "message": "Bank account successfully linked for autopay"
    }


@frappe.whitelist()
def get_authorization_status(loan):
    """
    Get the effective ACH authorization for a loan.

    Uses resolution logic:
    1. Check loan.ach_payment_account (if active)
    2. Fall back to customer's default (if active)

    Args:
        loan: Loan name

    Returns:
        dict with has_authorization, status, bank_name, account_last4, authorization_name, is_override
    """
    from payments_for_lending.payments_for_lending.doctype.ach_authorization.ach_authorization import get_loan_payment_account

    auth = get_loan_payment_account(loan)

    if auth:
        # Check if this is an override or using default
        loan_doc = frappe.get_doc("Loan", loan)
        is_override = bool(loan_doc.get("ach_payment_account"))

        return {
            "has_authorization": True,
            "authorization_name": auth.name,
            "status": auth.status,
            "bank_name": auth.bank_name,
            "account_last4": auth.bank_account_last4,
            "account_type": auth.account_type,
            "is_default": auth.is_default,
            "is_override": is_override,
            "token_source": auth.token_source,
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


# =============================================================================
# Multi-Account Management APIs
# =============================================================================

@frappe.whitelist()
def get_customer_accounts(customer):
    """
    Get all bank accounts for a customer.

    Args:
        customer: Customer name

    Returns:
        dict with accounts list
    """
    accounts = frappe.get_all(
        "ACH Authorization",
        filters={
            "customer": customer,
            "status": ["in", ["Active", "Paused"]]
        },
        fields=[
            "name", "status", "bank_name", "bank_account_last4",
            "account_type", "is_default", "token_source", "authorization_date"
        ],
        order_by="is_default desc, authorization_date desc"
    )

    return {
        "success": True,
        "accounts": accounts,
        "count": len(accounts)
    }


@frappe.whitelist()
def set_default_account(authorization_name):
    """
    Set a bank account as the default for the customer.

    Args:
        authorization_name: ACH Authorization name

    Returns:
        dict with success
    """
    auth = frappe.get_doc("ACH Authorization", authorization_name)
    auth.set_as_default()
    return {"success": True, "message": "Account set as default"}


@frappe.whitelist()
def set_loan_account(loan, authorization_name):
    """
    Set a specific bank account for a loan (override default).

    Args:
        loan: Loan name
        authorization_name: ACH Authorization name (or empty to clear override)

    Returns:
        dict with success
    """
    loan_doc = frappe.get_doc("Loan", loan)

    if authorization_name:
        # Validate the authorization belongs to the loan's customer
        auth = frappe.get_doc("ACH Authorization", authorization_name)
        if auth.customer != loan_doc.applicant:
            frappe.throw(_("This bank account does not belong to this customer"))
        if auth.status != "Active":
            frappe.throw(_("This bank account is not active"))

        loan_doc.ach_payment_account = authorization_name
    else:
        # Clear the override
        loan_doc.ach_payment_account = None

    loan_doc.save()
    frappe.db.commit()

    return {"success": True, "message": "Loan payment account updated"}


@frappe.whitelist()
def get_loan_account_info(loan):
    """
    Get the effective payment account info for a loan with resolution details.

    Args:
        loan: Loan name

    Returns:
        dict with account info and resolution source
    """
    from payments_for_lending.payments_for_lending.doctype.ach_authorization.ach_authorization import get_loan_payment_account

    loan_doc = frappe.get_doc("Loan", loan)
    auth = get_loan_payment_account(loan_doc)

    if not auth:
        return {
            "has_account": False,
            "resolution": "none",
            "message": "No payment account configured"
        }

    # Determine resolution source
    if loan_doc.get("ach_payment_account"):
        resolution = "loan_override"
    else:
        resolution = "customer_default"

    return {
        "has_account": True,
        "authorization_name": auth.name,
        "bank_name": auth.bank_name,
        "account_last4": auth.bank_account_last4,
        "account_type": auth.account_type,
        "status": auth.status,
        "is_default": auth.is_default,
        "token_source": auth.token_source,
        "resolution": resolution
    }


# =============================================================================
# Plaid Integration APIs
# =============================================================================

@frappe.whitelist()
def get_plaid_link_token(customer):
    """
    Get a Plaid Link token for the frontend.

    This initiates the Plaid Link flow. The token is used by the frontend
    to open Plaid Link UI.

    Args:
        customer: Customer name

    Returns:
        dict with link_token
    """
    settings = frappe.get_single("ACH Settings")

    if not settings.has_plaid_credentials():
        frappe.throw(_("Plaid is not configured"))

    # Get Plaid API credentials
    plaid_client_id = settings.plaid_client_id
    plaid_secret = settings.get_password("plaid_secret")
    plaid_base_url = settings.get_plaid_base_url()

    # Get customer info for Plaid
    customer_doc = frappe.get_doc("Customer", customer)

    try:
        response = requests.post(
            f"{plaid_base_url}/link/token/create",
            json={
                "client_id": plaid_client_id,
                "secret": plaid_secret,
                "user": {
                    "client_user_id": customer
                },
                "client_name": frappe.get_single("System Settings").company or "Payment System",
                "products": ["auth"],
                "country_codes": ["US"],
                "language": "en",
                "account_filters": {
                    "depository": {
                        "account_subtypes": ["checking", "savings"]
                    }
                }
            },
            timeout=30
        )
        response.raise_for_status()
        result = response.json()

        return {
            "success": True,
            "link_token": result.get("link_token"),
            "expiration": result.get("expiration")
        }

    except requests.RequestException as e:
        frappe.log_error(f"Plaid link token request failed: {str(e)}", "Plaid Integration")
        frappe.throw(_("Failed to initialize bank connection. Please try again."))


@frappe.whitelist()
def process_plaid_callback(public_token, account_id, customer, is_default=True):
    """
    Process the Plaid Link callback.

    After user completes Plaid Link:
    1. Exchange public_token for access_token
    2. Create processor_token for ACHQ
    3. Get account details
    4. Create ACH Authorization

    Args:
        public_token: Plaid public_token from Link callback
        account_id: Selected account ID from Plaid
        customer: Customer name
        is_default: Set as default payment account (default True)

    Returns:
        dict with success, authorization_name, bank_name, account_last4
    """
    settings = frappe.get_single("ACH Settings")

    if not settings.has_plaid_credentials():
        frappe.throw(_("Plaid is not configured"))

    plaid_client_id = settings.plaid_client_id
    plaid_secret = settings.get_password("plaid_secret")
    plaid_base_url = settings.get_plaid_base_url()

    try:
        # Step 1: Exchange public_token for access_token
        exchange_response = requests.post(
            f"{plaid_base_url}/item/public_token/exchange",
            json={
                "client_id": plaid_client_id,
                "secret": plaid_secret,
                "public_token": public_token
            },
            timeout=30
        )
        exchange_response.raise_for_status()
        exchange_result = exchange_response.json()
        access_token = exchange_result.get("access_token")

        # Step 2: Get account details
        accounts_response = requests.post(
            f"{plaid_base_url}/accounts/get",
            json={
                "client_id": plaid_client_id,
                "secret": plaid_secret,
                "access_token": access_token
            },
            timeout=30
        )
        accounts_response.raise_for_status()
        accounts_result = accounts_response.json()

        # Find the selected account
        account_info = None
        for acc in accounts_result.get("accounts", []):
            if acc.get("account_id") == account_id:
                account_info = acc
                break

        if not account_info:
            frappe.throw(_("Selected account not found"))

        # Step 3: Create processor token for ACHQ
        processor_response = requests.post(
            f"{plaid_base_url}/processor/token/create",
            json={
                "client_id": plaid_client_id,
                "secret": plaid_secret,
                "access_token": access_token,
                "account_id": account_id,
                "processor": "achq"
            },
            timeout=30
        )
        processor_response.raise_for_status()
        processor_result = processor_response.json()
        processor_token = processor_result.get("processor_token")

        # Get institution info
        institution = accounts_result.get("item", {}).get("institution_id", "")
        bank_name = ""
        if institution:
            try:
                inst_response = requests.post(
                    f"{plaid_base_url}/institutions/get_by_id",
                    json={
                        "client_id": plaid_client_id,
                        "secret": plaid_secret,
                        "institution_id": institution,
                        "country_codes": ["US"]
                    },
                    timeout=30
                )
                inst_response.raise_for_status()
                bank_name = inst_response.json().get("institution", {}).get("name", "")
            except (requests.RequestException, KeyError, ValueError):
                pass  # Bank name is optional

        # Convert is_default to boolean
        is_default = is_default in [True, 1, "1", "true", "True"]

        # Step 4: Create ACH Authorization
        auth = frappe.new_doc("ACH Authorization")
        auth.customer = customer
        auth.is_default = 1 if is_default else 0
        auth.status = "Active"
        auth.token_source = "Plaid"
        auth.bank_name = bank_name or account_info.get("name", "")
        auth.account_type = "Checking" if account_info.get("subtype") == "checking" else "Savings"
        auth.bank_account_last4 = account_info.get("mask", "")[-4:] if account_info.get("mask") else ""
        auth.routing_number_last4 = ""  # Not available from Plaid directly
        auth.achq_token = processor_token
        auth.verification_status = "POS"  # Plaid-verified accounts are considered positive
        auth.consent_captured = 1
        auth.authorization_ip = frappe.local.request_ip if hasattr(frappe.local, 'request_ip') else ""
        auth.authorization_date = now_datetime()
        auth.sec_code = settings.default_sec_code
        auth.insert()

        frappe.db.commit()

        return {
            "success": True,
            "authorization_name": auth.name,
            "bank_name": auth.bank_name,
            "account_last4": auth.bank_account_last4,
            "account_type": auth.account_type,
            "is_default": auth.is_default,
            "message": "Bank account successfully connected via Plaid"
        }

    except requests.RequestException as e:
        frappe.log_error(f"Plaid callback processing failed: {str(e)}", "Plaid Integration")
        frappe.throw(_("Failed to connect bank account. Please try again."))


@frappe.whitelist()
def is_plaid_available():
    """
    Check if Plaid integration is available and configured.

    Returns:
        dict with available boolean and environment
    """
    from payments_for_lending.payments_for_lending.doctype.ach_settings.ach_settings import is_plaid_enabled

    settings = frappe.get_single("ACH Settings")

    return {
        "available": is_plaid_enabled(),
        "environment": settings.plaid_environment if settings.has_plaid_credentials() else None
    }

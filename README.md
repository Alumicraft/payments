# Stripe Payment Integration for ERPNext

A custom Frappe app that integrates Stripe Invoicing for payment collection with persistent payment links, optimal pricing for low-volume businesses (≤25 invoices/month FREE), and support for Net 30/60 payment terms.

## Features

- **Persistent Payment Links** - Never expire, perfect for Net 30/60 terms
- **ACH Direct Debit (us_bank_account)** - 0.8% capped at $5 per transaction
- **Optional Card Payments** - 3% flat surcharge (controlled by toggle)
- **First 25 paid invoices per month FREE** (Stripe Invoicing Starter)
- **Automatic Payment Entry creation** in ERPNext when invoices are paid
- **US customers only** - International customers get wire transfer details

## Installation

### Via Frappe Cloud (Recommended)

1. Log into Frappe Cloud dashboard
2. Navigate to your site → Apps → Install App
3. Choose "Install from GitHub"
4. Enter repository URL: `https://github.com/your-org/stripe_payment_integration`
5. Select branch/tag and click Install

### Local Development

```bash
cd frappe-bench
bench get-app https://github.com/your-org/stripe_payment_integration
bench --site your-site.local install-app stripe_payment_integration
bench migrate
```

## Configuration

### 1. Stripe Account Setup

1. Create a Stripe account at [stripe.com](https://stripe.com)
2. Get your API keys from Dashboard → Developers → API keys
3. Copy both Secret Key and Publishable Key

### 2. Configure Stripe Settings in ERPNext

1. Go to: **Stripe Settings** (Search in awesome bar)
2. Enter your Stripe API Key (Secret Key)
3. Enter your Publishable Key
4. Save

### 3. Configure Webhook in Stripe Dashboard

1. Go to Stripe Dashboard → Developers → Webhooks
2. Click "Add endpoint"
3. Enter endpoint URL:
   ```
   https://your-site.frappe.cloud/api/method/stripe_payment_integration.webhook.handle_stripe_webhook
   ```
4. Select events:
   - `invoice.paid`
   - `invoice.payment_failed`
   - `invoice.voided`
   - `invoice.payment_action_required`
   - `payment_intent.succeeded`
5. Copy the Webhook Signing Secret
6. Add it to Stripe Settings in ERPNext

## Usage

### Creating a Payment Request

1. Create a new Payment Request in ERPNext
2. Set the party (Customer) and amount
3. Toggle "Allow Card Payment" if you want to offer card payments (with 3% fee)
4. Save - a Stripe Invoice will be created automatically
5. The Stripe Invoice URL will appear in the form

### Payment Options

| Payment Method | Fee |
|----------------|-----|
| ACH Direct Debit | 0.8% (max $5) |
| Card | 3% flat (passed to customer) |

### International Customers

Customers with non-US addresses will see wire transfer details instead of online payment options.

## Custom Fields Added

### Payment Request
- `stripe_invoice_url` - Stripe hosted invoice URL
- `stripe_invoice_id` - Stripe Invoice ID
- `stripe_payment_status` - Pending/Paid/Failed/Voided
- `stripe_payment_intent_id` - For reconciliation
- `allow_card_payment` - Toggle for card payments
- `card_processing_fee` - Calculated 3% fee
- `total_with_card_fee` - Amount including card fee

### Customer
- `stripe_customer_id` - Links ERPNext Customer to Stripe Customer

## License

MIT License - See LICENSE file

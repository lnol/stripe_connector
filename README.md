# Stripe Connector

Fetch Stripe invoices and credit notes into Odoo Accounting.

## What it does

- Periodic (or on-demand) sync of finalized Stripe invoices and issued credit
  notes into `account.move`.
- Resolves Stripe customers to `res.partner` and Stripe products to
  `product.template`, creating them on the fly when missing. New customers
  carry over the Stripe business name, email, phone, postal address, taxation
  country (preferring `customer.tax.location.country` over the postal address),
  and the first VAT ID from `customer.tax_ids` (which also flips
  `is_company=True`).
- Attaches the Stripe-rendered PDF to the move.
- Multi-currency: each move is created with the currency from the Stripe object.
- Multi-account, multi-company: each `stripe.account` is scoped to a company
  and isolated by record rule.
- Per-run history with per-object pass/skip/fail lines, surfaced as a chatter
  thread on each `stripe.import.run`.

## What it does **not** do

- **Taxes are not imported.** Invoices land in `draft`. A human is expected to
  edit each move and add tax lines so the Odoo total matches the Stripe total
  before posting. If you turn on *Auto-Confirm Invoices* on a Stripe Account,
  posting happens without that human review — only enable it when your Stripe
  account does not charge tax.
- **No payment reconciliation.** This module brings invoices in; it does not
  match them to bank statements. Pair with `account_online_synchronization` or
  manual reconciliation.

## Requirements

- Odoo 19.0 (Community is sufficient — only `account` and `mail` are required).
- A Stripe secret API key with read access to invoices, credit notes,
  customers, and PDFs.
- Python `requests` (already a dependency of Odoo core).

## Installation

1. Install the module through the odoo app page.
2. Grant *Stripe Administrator* to whichever user manages API keys.
   *Accounting Manager* implies it; *Accounting User* implies the lighter
   *Stripe User* (read-only).

## Configuration

*Accounting → Configuration → Stripe Accounts*. Create one record per Stripe
account you want to import from:

| Field | Required | Notes |
|---|---|---|
| **Name** | yes | Free-text label. |
| **API Secret Key** | yes | Stripe secret key (`sk_live_…` / `sk_test_…`). Stored field-level-restricted to *Stripe Administrator*. |
| **Company** | yes | Multi-company isolation. |
| **Sales Journal** | yes | Where imported invoices and credit notes land. |
| **Bank/Cash Journal** | no | Currently informational; future-use for reconciliation. |
| **Default Revenue Account** | no | Fallback when the resolved product has no income account set. |
| **Auto-Confirm Invoices** | no, default `off` | Posts the move on import. **Leave off unless your Stripe account is tax-free** — see *What it does not do*. |
| **Cut-off Date** | no | Stripe invoices finalized strictly before this date are ignored. Use this when going live to skip historical data already booked manually. |
| **Fetch Lookback (days)** | no, default `90` | On every fetch, also re-scan invoices created within this many days. Catches subscription invoices that were created earlier but only finalized recently. Already-imported invoices are deduplicated by SQL UNIQUE. Increase if your subscription drafts can sit unfinalized for longer than 90 days. |

## Usage

### Scheduled

`Stripe: Fetch Invoices` runs daily as `base.user_root`. It iterates every
active `stripe.account`, processes each in isolation (one failure does not
block the others), and writes a `stripe.import.run` record per account.

### Manual

Each sales journal that has a linked active Stripe account shows a
**Fetch Stripe Invoices** button on its dashboard kanban card. Clicking it
triggers the shared cron to fire on its next poll cycle (≤60 s). Only members
of *Stripe Administrator* see and can use the button.

### Import history

*Accounting → Stripe Import Runs*. Each run carries:

- Counts of invoices / credit notes seen, imported, skipped, failed.
- A summary message and chatter thread.
- Per-object lines (`stripe.import.run.line`) with the source Stripe ID and
  the resulting `account.move`.

Failed runs do **not** advance the per-account watermark, so a transient
failure is retried on the next fetch.

If a previous worker crashes mid-fetch, the leftover `running` record is
marked `failed` at the start of the next fetch.

## Security model

- **Stripe User** (implied by *Accounting User*): read access to
  `stripe.account`, `stripe.import.run`, and the Stripe-ID fields on related
  records.
- **Stripe Administrator** (implied by *Accounting Manager*): full CRUD on
  Stripe Accounts and Import Runs; only group that can write the Stripe ID
  fields on `res.partner`, `product.template`, and `account.move`.
- Record rules restrict `stripe.account` and import runs to
  `company_id in user companies`.
- The API key is field-level-restricted (`groups='…group_stripe_admin'`) so
  non-admins cannot read it back even if they get a record reference.

## Stripe object reference

Stat buttons on partner / product / invoice forms link directly to the Stripe
dashboard:

- Partner → `https://dashboard.stripe.com/customers/<id>`
- Product → `https://dashboard.stripe.com/products/<id>`
- Invoice → `https://dashboard.stripe.com/invoices/<id>`
- Credit note → `https://dashboard.stripe.com/credit_notes/<id>`


## Known limitations

- First fetch with no `Cut-off Date` walks every invoice in the Stripe
  account. Set a cut-off before installing in a long-lived Stripe account.
- The Stripe API list endpoint cannot filter by `finalized_at`; the lookback
  window above is the workaround. If draft-to-finalize windows can exceed 90
  days for your business, raise *Fetch Lookback (days)*.
- No webhook ingestion. Latency is bounded by the cron interval.



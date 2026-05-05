from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from odoo import SUPERUSER_ID, fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'stripe_connector')
class TestStripeAccount(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.sales_journal = cls.env['account.journal'].create({
            'name': 'Test Stripe Primary Sales',
            'code': 'TSPSL',
            'type': 'sale',
            'company_id': cls.company.id,
        })
        cls.revenue_account = cls.env['account.account'].search([
            ('account_type', '=', 'income'),
            ('company_ids', 'in', [cls.company.id]),
        ], limit=1)
        # Archive any pre-existing stripe accounts so the cron-iteration tests
        # don't pick up real production records that might exist on this DB.
        # The class-level savepoint restores them when the class tears down.
        cls.env['stripe.account'].with_context(active_test=False).search([]).write({'active': False})
        cls.stripe_account = cls.env['stripe.account'].create({
            'name': 'Test Stripe',
            'api_key': 'sk_test_dummy',
            'stripe_account_identifier': 'acct_TESTSTRIPE',
            'company_id': cls.company.id,
            'sales_journal_id': cls.sales_journal.id,
            'default_revenue_account_id': cls.revenue_account.id,
        })

    @contextmanager
    def _disable_vat_check(self):
        """Skip Odoo's VAT validation for the duration of the block.

        ``base_vat``'s ``_inverse_vat`` calls ``_check_vat`` which can reject
        Stripe-formatted VAT values that fail country-specific checksums.
        Tests focused on Stripe→Odoo extraction should not depend on those
        rules; if ``base_vat`` is not installed this is a no-op.
        """
        Partner = type(self.env['res.partner'])
        if not hasattr(Partner, '_inverse_vat'):
            yield
            return
        with patch.object(Partner, '_inverse_vat', lambda partner_self: None):
            yield

    def _create_sales_journal(self, company=None, name='Stripe Test Sales'):
        company = company or self.company
        journal_number = self.env['account.journal'].search_count([
            ('company_id', '=', company.id),
        ]) + 1
        return self.env['account.journal'].create({
            'name': '%s %s' % (name, journal_number),
            'code': 'S%04d' % journal_number,
            'type': 'sale',
            'company_id': company.id,
        })

    # ── _resolve_partner ────────────────────────────────────────────────

    def test_resolve_partner_creates_new(self):
        service = MagicMock()
        service.get_customer.return_value = {
            'name': 'Acme Corp',
            'email': 'acme@example.com',
            'phone': '+1234567890',
        }
        partner = self.stripe_account._resolve_partner('cus_NEW123', service)
        self.assertEqual(partner.stripe_customer_id, 'cus_NEW123')
        self.assertEqual(partner.name, 'Acme Corp')
        self.assertEqual(partner.email, 'acme@example.com')
        self.assertEqual(partner.customer_rank, 1)
        self.assertEqual(partner.stripe_account_id, self.stripe_account)
        service.get_customer.assert_called_once_with('cus_NEW123')

    def test_resolve_partner_returns_existing(self):
        existing = self.env['res.partner'].create({
            'name': 'Existing Corp',
            'stripe_customer_id': 'cus_EXIST456',
        })
        service = MagicMock()
        partner = self.stripe_account._resolve_partner('cus_EXIST456', service)
        self.assertEqual(partner.id, existing.id)
        self.assertEqual(partner.stripe_account_id, self.stripe_account)
        service.get_customer.assert_not_called()

    def test_resolve_partner_uses_email_as_name_fallback(self):
        service = MagicMock()
        service.get_customer.return_value = {
            'name': '',
            'email': 'fallback@example.com',
            'phone': '',
        }
        partner = self.stripe_account._resolve_partner('cus_NONAME', service)
        self.assertEqual(partner.name, 'fallback@example.com')

    def test_resolve_partner_populates_address(self):
        service = MagicMock()
        service.get_customer.return_value = {
            'name': 'Acme GmbH',
            'email': 'billing@acme.de',
            'phone': '+49 30 1234567',
            'address': {
                'line1': 'Hauptstr. 1',
                'line2': 'Hinterhof',
                'city': 'Berlin',
                'postal_code': '10115',
                'country': 'DE',
            },
        }
        partner = self.stripe_account._resolve_partner('cus_DE001', service)
        self.assertEqual(partner.street, 'Hauptstr. 1')
        self.assertEqual(partner.street2, 'Hinterhof')
        self.assertEqual(partner.city, 'Berlin')
        self.assertEqual(partner.zip, '10115')
        self.assertEqual(partner.country_id.code, 'DE')

    def test_resolve_partner_uses_tax_country_over_address_country(self):
        service = MagicMock()
        service.get_customer.return_value = {
            'name': 'Cross Border Co',
            'email': 'cb@example.com',
            'address': {
                'line1': '1 Wall St',
                'country': 'US',
            },
            'tax': {
                'location': {'country': 'GB', 'source': 'shipping_destination'},
            },
        }
        partner = self.stripe_account._resolve_partner('cus_TAXCOUNTRY', service)
        self.assertEqual(partner.country_id.code, 'GB')

    def test_resolve_partner_falls_back_to_address_country(self):
        service = MagicMock()
        service.get_customer.return_value = {
            'name': 'Address Only',
            'email': 'a@example.com',
            'address': {'line1': '14 Rue de Rivoli', 'country': 'FR'},
        }
        partner = self.stripe_account._resolve_partner('cus_FR001', service)
        self.assertEqual(partner.country_id.code, 'FR')

    def test_resolve_partner_populates_vat_from_tax_ids(self):
        # The intent of this test is the Stripe→Odoo extraction, not Odoo's
        # checksum validation; patch ``_inverse_vat`` so a structurally valid
        # but checksum-invalid VAT is accepted as-is.
        service = MagicMock()
        service.get_customer.return_value = {
            'name': 'VAT Co',
            'email': 'vat@example.com',
            'address': {'country': 'DE'},
            'tax_ids': {
                'object': 'list',
                'data': [
                    {'id': 'txi_1', 'type': 'eu_vat', 'value': 'DE123456789'},
                ],
            },
        }
        with self._disable_vat_check():
            partner = self.stripe_account._resolve_partner('cus_VAT001', service)
        self.assertEqual(partner.vat, 'DE123456789')
        self.assertTrue(partner.is_company)

    def test_resolve_partner_handles_bare_tax_id_list(self):
        service = MagicMock()
        service.get_customer.return_value = {
            'name': 'Bare List Co',
            'email': 'bare@example.com',
            'address': {'country': 'AT'},
            'tax_ids': [
                {'id': 'txi_2', 'type': 'eu_vat', 'value': 'ATU12345678'},
            ],
        }
        with self._disable_vat_check():
            partner = self.stripe_account._resolve_partner('cus_BARE', service)
        self.assertEqual(partner.vat, 'ATU12345678')

    def test_resolve_partner_drops_invalid_vat_and_retries(self):
        """Simulate base_vat rejecting the VAT — partner is still created."""
        service = MagicMock()
        service.get_customer.return_value = {
            'name': 'Bad VAT Co',
            'email': 'badvat@example.com',
            'address': {'country': 'NL'},
            'tax_ids': {
                'object': 'list',
                'data': [{'id': 'txi_x', 'type': 'eu_vat', 'value': 'NL_BAD_VAT'}],
            },
        }
        Partner = type(self.env['res.partner'])
        original_create = Partner.create
        seen = []

        def stub_create(self_, vals_list):
            normalized = vals_list if isinstance(vals_list, list) else [vals_list]
            seen.append([dict(v) for v in normalized])
            if any(v.get('vat') for v in normalized):
                raise ValidationError('VAT NL_BAD_VAT does not seem to be valid.')
            return original_create(self_, vals_list)

        with patch.object(Partner, 'create', stub_create):
            partner = self.stripe_account._resolve_partner('cus_BADVAT', service)

        self.assertFalse(partner.vat)
        self.assertTrue(partner.is_company)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0].get('vat'), 'NL_BAD_VAT')
        self.assertNotIn('vat', seen[1][0])

    def test_resolve_partner_no_tax_ids_does_not_touch_company_flag(self):
        service = MagicMock()
        service.get_customer.return_value = {
            'name': 'Individual',
            'email': 'me@example.com',
        }
        partner = self.stripe_account._resolve_partner('cus_INDIV', service)
        self.assertFalse(partner.vat)
        self.assertFalse(partner.is_company)

    # ── _resolve_product ────────────────────────────────────────────────

    def test_resolve_product_creates_new(self):
        product = self.stripe_account._resolve_product('prod_NEW001', 'Widget Pro')
        self.assertEqual(product.stripe_product_id, 'prod_NEW001')
        self.assertEqual(product.stripe_account_id, self.stripe_account)
        self.assertEqual(product.name, 'Widget Pro')
        self.assertEqual(product.type, 'service')
        self.assertTrue(product.sale_ok)
        self.assertFalse(product.purchase_ok)

    def test_resolve_product_strips_invoice_line_quantity_and_price(self):
        product = self.stripe_account._resolve_product(
            'prod_CLEAN001',
            '1 \u00d7 Consulting services for Digital Marketing via Google Ads '
            '(at \u20ac750.00 / month)',
        )

        self.assertEqual(
            product.name,
            'Consulting services for Digital Marketing via Google Ads',
        )

    def test_resolve_product_returns_existing(self):
        existing = self.env['product.template'].create({
            'name': 'Existing Widget',
            'stripe_product_id': 'prod_EXIST002',
            'type': 'service',
        })
        product = self.stripe_account._resolve_product('prod_EXIST002', 'Ignored Name')
        self.assertEqual(product.id, existing.id)
        self.assertEqual(product.stripe_account_id, self.stripe_account)

    def test_stripe_account_identifier_rejects_invalid_value(self):
        invalid_journal = self._create_sales_journal(name='Invalid Stripe Sales')
        with self.assertRaises(ValidationError):
            self.env['stripe.account'].create({
                'name': 'Invalid Stripe ID',
                'api_key': 'sk_test_invalid',
                'stripe_account_identifier': 'invalid_account_id',
                'company_id': self.company.id,
                'sales_journal_id': invalid_journal.id,
            })

    # ── _get_line_product_id ────────────────────────────────────────────

    def test_stripe_account_identifier_is_trimmed_on_create(self):
        extra_journal = self.env['account.journal'].create({
            'name': 'Trimmed Create Sales',
            'code': 'TRMC',
            'type': 'sale',
            'company_id': self.company.id,
        })
        account = self.env['stripe.account'].create({
            'name': 'Trimmed Create Stripe',
            'api_key': 'sk_test_trimmed_create',
            'stripe_account_identifier': '  acct_TRIMMEDCREATE  ',
            'company_id': self.company.id,
            'sales_journal_id': extra_journal.id,
        })

        self.assertEqual(account.stripe_account_identifier, 'acct_TRIMMEDCREATE')

    def test_stripe_account_identifier_is_trimmed_on_write(self):
        self.stripe_account.write({
            'stripe_account_identifier': '  acct_TRIMMEDWRITE  ',
        })

        self.assertEqual(self.stripe_account.stripe_account_identifier, 'acct_TRIMMEDWRITE')

    def test_get_line_product_id_new_api(self):
        line = {
            'pricing': {
                'price_details': {
                    'product': 'prod_NEW_API',
                }
            }
        }
        self.assertEqual(self.stripe_account._get_line_product_id(line), 'prod_NEW_API')

    def test_get_line_product_id_legacy_api(self):
        line = {
            'pricing': {},
            'price': {'product': 'prod_LEGACY'},
        }
        self.assertEqual(self.stripe_account._get_line_product_id(line), 'prod_LEGACY')

    def test_get_line_product_id_missing(self):
        self.assertIsNone(self.stripe_account._get_line_product_id({}))

    # ── _process_stripe_invoice deduplication ───────────────────────────

    def test_process_invoice_skips_duplicate(self):
        existing_move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'journal_id': self.sales_journal.id,
            'stripe_invoice_id': 'in_ALREADY_IMPORTED',
        })
        service = MagicMock()
        stripe_invoice = {'id': 'in_ALREADY_IMPORTED', 'customer': 'cus_X'}
        self.stripe_account._process_stripe_invoice(stripe_invoice, service)
        self.assertEqual(existing_move.stripe_account_id, self.stripe_account)
        service.get_customer.assert_not_called()

    def test_process_invoice_skips_no_customer(self):
        service = MagicMock()
        stripe_invoice = {'id': 'in_NO_CUSTOMER', 'customer': None}
        with self.assertRaises(ValueError):
            self.stripe_account._process_stripe_invoice(stripe_invoice, service)
        service.get_customer.assert_not_called()
        move_count = self.env['account.move'].search_count([
            ('stripe_invoice_id', '=', 'in_NO_CUSTOMER'),
        ])
        self.assertEqual(move_count, 0)

    def test_process_invoice_creates_move(self):
        service = MagicMock()
        service.get_customer.return_value = {
            'name': 'New Customer',
            'email': 'new@example.com',
            'phone': '',
        }
        service.get_invoice_lines.return_value = [{
            'amount': 5000,
            'description': 'Service Fee',
            'quantity': 1,
            'pricing': {'price_details': {'product': 'prod_SVC'}},
        }]
        service.get_pdf.return_value = b'%PDF-fake'

        stripe_invoice = {
            'id': 'in_NEWTEST001',
            'customer': 'cus_NEWTEST',
            'status': 'paid',
            'created': 1700000000,
            'due_date': 1702592000,
            'invoice_pdf': 'https://stripe.example.com/invoice.pdf',
        }
        self.stripe_account._process_stripe_invoice(stripe_invoice, service, move_type='out_invoice')

        move = self.env['account.move'].search([('stripe_invoice_id', '=', 'in_NEWTEST001')])
        self.assertEqual(len(move), 1)
        self.assertEqual(move.move_type, 'out_invoice')
        self.assertEqual(move.journal_id, self.sales_journal)
        self.assertEqual(move.stripe_account_id, self.stripe_account)
        self.assertEqual(move.state, 'draft')

        # Verify line was created with correct price (50.00 EUR/USD from 5000 cents)
        self.assertEqual(len(move.invoice_line_ids), 1)
        self.assertAlmostEqual(move.invoice_line_ids[0].price_unit, 50.0)

    def test_get_line_price_unit_divides_total_amount_by_quantity(self):
        line = {
            'amount': 3000,
            'quantity': 3,
        }
        self.assertAlmostEqual(self.stripe_account._get_line_price_unit(line), 10.0)

    def test_get_line_price_unit_uses_unit_amount_when_present(self):
        line = {
            'amount': 3000,
            'quantity': 3,
            'pricing': {
                'unit_amount_decimal': '1000',
            },
        }
        self.assertAlmostEqual(self.stripe_account._get_line_price_unit(line), 10.0)

    def test_prepare_move_line_vals_includes_deferred_period_when_available(self):
        period_start = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        period_end = int(datetime(2024, 1, 31, tzinfo=timezone.utc).timestamp())
        line = {
            'amount': 1000,
            'description': 'Recurring service',
            'quantity': 1,
            'period': {
                'start': period_start,
                'end': period_end,
            },
        }

        with patch.object(type(self.stripe_account), '_has_deferred_date_fields', return_value=True):
            line_vals = self.stripe_account._prepare_move_line_vals(line)

        self.assertEqual(line_vals['deferred_start_date'], datetime(2024, 1, 1).date())
        self.assertEqual(line_vals['deferred_end_date'], datetime(2024, 1, 31).date())

    def test_prepare_move_line_vals_ignores_partial_deferred_period(self):
        line = {
            'amount': 1000,
            'description': 'Recurring service',
            'quantity': 1,
            'period': {
                'start': int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()),
            },
        }

        with patch.object(type(self.stripe_account), '_has_deferred_date_fields', return_value=True):
            line_vals = self.stripe_account._prepare_move_line_vals(line)

        self.assertNotIn('deferred_start_date', line_vals)
        self.assertNotIn('deferred_end_date', line_vals)

    def test_prepare_move_line_vals_ignores_invalid_deferred_period_range(self):
        line = {
            'amount': 1000,
            'description': 'Recurring service',
            'quantity': 1,
            'period': {
                'start': int(datetime(2024, 1, 31, tzinfo=timezone.utc).timestamp()),
                'end': int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()),
            },
        }

        with patch.object(type(self.stripe_account), '_has_deferred_date_fields', return_value=True):
            line_vals = self.stripe_account._prepare_move_line_vals(line)

        self.assertNotIn('deferred_start_date', line_vals)
        self.assertNotIn('deferred_end_date', line_vals)

    def test_prepare_move_line_vals_ignores_invalid_deferred_period_timestamps(self):
        line = {
            'amount': 1000,
            'description': 'Recurring service',
            'quantity': 1,
            'period': {
                'start': 'invalid',
                'end': int(datetime(2024, 1, 31, tzinfo=timezone.utc).timestamp()),
            },
        }

        with patch.object(type(self.stripe_account), '_has_deferred_date_fields', return_value=True):
            line_vals = self.stripe_account._prepare_move_line_vals(line)

        self.assertNotIn('deferred_start_date', line_vals)
        self.assertNotIn('deferred_end_date', line_vals)

    def test_get_stripe_lines_adds_deferred_period_to_credit_note_lines(self):
        period = {
            'start': int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()),
            'end': int(datetime(2024, 1, 31, tzinfo=timezone.utc).timestamp()),
        }
        service = MagicMock()
        service.get_credit_note_lines.return_value = [{
            'id': 'cnli_PERIOD',
            'amount': 1000,
            'description': 'Refunded subscription',
            'quantity': 1,
            'invoice_line_item': 'il_PERIOD',
        }]
        service.get_invoice_lines.return_value = [{
            'id': 'il_PERIOD',
            'period': period,
        }]

        lines = self.stripe_account._get_stripe_lines(
            {'id': 'cn_PERIOD', 'invoice': 'in_PERIOD'},
            service,
            'credit_note',
        )

        self.assertEqual(lines[0]['period'], period)
        service.get_credit_note_lines.assert_called_once_with('cn_PERIOD')
        service.get_invoice_lines.assert_called_once_with('in_PERIOD')

    def test_get_stripe_lines_keeps_credit_note_period_when_present(self):
        period = {
            'start': int(datetime(2024, 2, 1, tzinfo=timezone.utc).timestamp()),
            'end': int(datetime(2024, 2, 29, tzinfo=timezone.utc).timestamp()),
        }
        service = MagicMock()
        service.get_credit_note_lines.return_value = [{
            'id': 'cnli_DIRECT_PERIOD',
            'amount': 1000,
            'description': 'Refunded subscription',
            'quantity': 1,
            'invoice_line_item': 'il_DIRECT_PERIOD',
            'period': period,
        }]

        lines = self.stripe_account._get_stripe_lines(
            {'id': 'cn_DIRECT_PERIOD', 'invoice': 'in_DIRECT_PERIOD'},
            service,
            'credit_note',
        )

        self.assertEqual(lines[0]['period'], period)
        service.get_credit_note_lines.assert_called_once_with('cn_DIRECT_PERIOD')
        service.get_invoice_lines.assert_not_called()

    def test_process_invoice_auto_confirm(self):
        self.stripe_account.auto_confirm = True
        service = MagicMock()
        service.get_customer.return_value = {
            'name': 'AutoPost Customer',
            'email': 'auto@example.com',
            'phone': '',
        }
        service.get_invoice_lines.return_value = [{
            'amount': 10000,
            'description': 'Subscription',
            'quantity': 1,
            'pricing': {'price_details': {'product': 'prod_AUTO_SUB'}},
        }]
        service.get_pdf.return_value = b''

        stripe_invoice = {
            'id': 'in_AUTOPOST001',
            'customer': 'cus_AUTOPOST',
            'status': 'paid',
            'created': 1700000000,
            'due_date': None,
            'invoice_pdf': None,
        }
        self.stripe_account._process_stripe_invoice(stripe_invoice, service, move_type='out_invoice')
        move = self.env['account.move'].search([('stripe_invoice_id', '=', 'in_AUTOPOST001')])
        self.assertEqual(move.state, 'posted')
        self.stripe_account.auto_confirm = False

    def test_process_invoice_sets_currency_from_stripe(self):
        usd = self.env.ref('base.USD')
        service = MagicMock()
        service.get_customer.return_value = {'name': 'FX Customer', 'email': '', 'phone': ''}
        service.get_invoice_lines.return_value = [{
            'amount': 5000,
            'description': 'FX line',
            'quantity': 1,
            'pricing': {'price_details': {'product': 'prod_FX'}},
        }]
        service.get_pdf.return_value = b''

        stripe_invoice = {
            'id': 'in_FX001',
            'customer': 'cus_FX',
            'status': 'paid',
            'created': 1700000000,
            'currency': 'usd',
            'invoice_pdf': None,
        }
        self.stripe_account._process_stripe_invoice(stripe_invoice, service, move_type='out_invoice')
        move = self.env['account.move'].search([('stripe_invoice_id', '=', 'in_FX001')])
        self.assertEqual(move.currency_id, usd)

    def test_process_invoice_uses_finalized_at_for_invoice_date(self):
        created_ts = int(datetime(2024, 1, 1, 0, 0, 0).timestamp())
        finalized_ts = int(datetime(2024, 3, 15, 12, 0, 0).timestamp())
        service = MagicMock()
        service.get_customer.return_value = {'name': 'Late Finalize', 'email': '', 'phone': ''}
        service.get_invoice_lines.return_value = [{
            'amount': 1000,
            'description': 'Late',
            'quantity': 1,
            'pricing': {'price_details': {'product': 'prod_LATE'}},
        }]
        service.get_pdf.return_value = b''

        stripe_invoice = {
            'id': 'in_LATEFIN001',
            'customer': 'cus_LATE',
            'status': 'paid',
            'created': created_ts,
            'status_transitions': {'finalized_at': finalized_ts},
            'currency': 'eur',
            'invoice_pdf': None,
        }
        self.stripe_account._process_stripe_invoice(stripe_invoice, service, move_type='out_invoice')
        move = self.env['account.move'].search([('stripe_invoice_id', '=', 'in_LATEFIN001')])
        self.assertEqual(
            move.invoice_date,
            datetime.fromtimestamp(finalized_ts, tz=timezone.utc).date(),
        )

    def test_get_invoice_date_falls_back_to_created(self):
        result = self.stripe_account._get_invoice_date({'created': 1700000000})
        self.assertEqual(result, datetime.fromtimestamp(1700000000, tz=timezone.utc).date())

    def test_process_credit_note_creates_out_refund(self):
        service = MagicMock()
        service.get_customer.return_value = {
            'name': 'Refund Customer',
            'email': 'refund@example.com',
            'phone': '',
        }
        service.get_credit_note_lines.return_value = [{
            'amount': 2000,
            'description': 'Refund',
            'quantity': 1,
            'pricing': {},
        }]
        service.get_pdf.return_value = b''

        stripe_cn = {
            'id': 'cn_TESTCN001',
            'customer': 'cus_REFUNDTEST',
            'status': 'issued',
            'created': 1700000000,
            'due_date': None,
            'pdf': None,
        }
        self.stripe_account._process_stripe_invoice(stripe_cn, service, move_type='out_refund')
        move = self.env['account.move'].search([('stripe_invoice_id', '=', 'cn_TESTCN001')])
        self.assertEqual(move.move_type, 'out_refund')
        self.assertEqual(move.stripe_account_id, self.stripe_account)
        self.assertEqual(move.stripe_object_type, 'credit_note')
        service.get_credit_note_lines.assert_called_once_with('cn_TESTCN001')
        service.get_invoice_lines.assert_not_called()

    # ── PDF attachment ──────────────────────────────────────────────────

    def test_attach_pdf_creates_attachment(self):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'journal_id': self.sales_journal.id,
        })
        service = MagicMock()
        service.get_pdf.return_value = b'%PDF-test-content'

        self.stripe_account._attach_pdf(
            move,
            {'id': 'in_PDF_TEST', 'invoice_pdf': 'https://stripe.example.com/pdf'},
            service,
            pdf_field='invoice_pdf',
        )
        attachment = self.env['ir.attachment'].search([
            ('res_model', '=', 'account.move'),
            ('res_id', '=', move.id),
        ])
        self.assertEqual(len(attachment), 1)
        self.assertIn('in_PDF_TEST', attachment.name)

    def test_attach_pdf_graceful_failure(self):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'journal_id': self.sales_journal.id,
        })
        service = MagicMock()
        service.get_pdf.side_effect = Exception('Network error')

        # Should not raise
        self.stripe_account._attach_pdf(
            move,
            {'id': 'in_PDF_FAIL', 'invoice_pdf': 'https://stripe.example.com/fail'},
            service,
            pdf_field='invoice_pdf',
        )
        attachment_count = self.env['ir.attachment'].search_count([
            ('res_model', '=', 'account.move'),
            ('res_id', '=', move.id),
        ])
        self.assertEqual(attachment_count, 0)

    def test_prefetch_data_does_not_download_pdf(self):
        service = MagicMock()
        service.get_customer.return_value = {'name': 'PDF Later', 'email': '', 'phone': ''}
        service.get_invoice_lines.return_value = []

        prefetched = self.stripe_account._prefetch_stripe_data(
            {
                'id': 'in_PREFETCH_NO_PDF',
                'customer': 'cus_PREFETCH_NO_PDF',
                'invoice_pdf': 'https://stripe.example.com/invoice.pdf',
            },
            service,
            'invoice',
        )

        self.assertIn('stripe_lines', prefetched)
        self.assertNotIn('pdf_bytes', prefetched)
        service.get_pdf.assert_not_called()

    def test_process_batch_item_commits_before_pdf_attachment(self):
        service = MagicMock()
        service.get_customer.return_value = {'name': 'Committed First', 'email': '', 'phone': ''}
        service.get_invoice_lines.return_value = [{
            'amount': 1000,
            'description': 'Committed line',
            'quantity': 1,
            'pricing': {'price_details': {'product': 'prod_COMMIT_FIRST'}},
        }]
        run = self.env['stripe.import.run'].create({
            'stripe_account_id': self.stripe_account.id,
            'state': 'running',
            'message': 'Running',
        })
        events = []

        def fake_commit(_account, processed=0, remaining=None):
            events.append(('commit', processed, remaining))
            return True

        def fake_attach(
            _account,
            move,
            _stripe_obj,
            service=None,
            pdf_field='invoice_pdf',
            pdf_bytes=None,
        ):
            events.append(('attach', bool(move.id), pdf_field, bool(service), pdf_bytes))

        with patch.object(
            type(self.stripe_account),
            '_commit_import_progress',
            autospec=True,
            side_effect=fake_commit,
        ), patch.object(
            type(self.stripe_account),
            '_attach_pdf',
            autospec=True,
            side_effect=fake_attach,
        ):
            state, failure, keep_going = self.stripe_account._process_batch_item(
                run,
                {
                    'id': 'in_COMMIT_BEFORE_PDF',
                    'customer': 'cus_COMMIT_BEFORE_PDF',
                    'status': 'paid',
                    'created': 1700000000,
                    'currency': 'eur',
                    'invoice_pdf': 'https://stripe.example.com/invoice.pdf',
                },
                service,
                'out_invoice',
                'invoice',
                remaining_after=7,
            )

        self.assertEqual(state, 'done')
        self.assertFalse(failure)
        self.assertTrue(keep_going)
        self.assertEqual([event[0] for event in events], ['commit', 'attach', 'commit'])
        self.assertEqual(events[0], ('commit', 1, 7))
        self.assertEqual(events[2], ('commit', 0, 7))

    # ── _fetch_invoices ─────────────────────────────────────────────────

    def test_fetch_invoices_updates_last_fetch_at(self):
        service = MagicMock()
        service.get_invoices.return_value = []
        service.get_credit_notes.return_value = []

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ):
            self.stripe_account._fetch_invoices()

        self.assertIsNotNone(self.stripe_account.last_fetch_at)

    def test_fetch_invoices_passes_last_fetch_at(self):
        self.stripe_account.last_fetch_at = fields.Datetime.to_datetime('2024-01-01 12:00:00')
        service = MagicMock()
        service.get_invoices.return_value = []
        service.get_credit_notes.return_value = []

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ):
            self.stripe_account._fetch_invoices()

        service.get_invoices.assert_called_once()
        call_args, call_kwargs = service.get_invoices.call_args
        created_after = call_kwargs.get('created_after')
        if created_after is None and call_args:
            created_after = call_args[0]
        self.assertIsNotNone(created_after)

    def test_fetch_invoices_passes_invoice_cutoff_date(self):
        cutoff_date = fields.Date.to_date('2024-02-01')
        self.stripe_account.invoice_cutoff_date = cutoff_date
        service = MagicMock()
        service.get_invoices.return_value = []
        service.get_credit_notes.return_value = []

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ):
            self.stripe_account._fetch_invoices()

        service.get_invoices.assert_called_once()
        self.assertEqual(
            service.get_invoices.call_args.kwargs.get('finalized_after'),
            cutoff_date,
        )

    def test_fetch_invoices_passes_credit_note_cutoff_date(self):
        cutoff_date = fields.Date.to_date('2024-02-01')
        self.stripe_account.last_fetch_at = False
        self.stripe_account.invoice_cutoff_date = cutoff_date
        service = MagicMock()
        service.get_invoices.return_value = []
        service.get_credit_notes.return_value = []

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ):
            self.stripe_account._fetch_invoices()

        service.get_credit_notes.assert_called_once()
        self.assertEqual(
            service.get_credit_notes.call_args.kwargs.get('created_on_or_after'),
            cutoff_date,
        )
        self.assertEqual(
            service.get_credit_notes.call_args.kwargs.get('created_after'),
            datetime(2024, 1, 31, 23, 59, 59),
        )

    def test_fetch_invoices_bulk_skips_existing_without_prefetch(self):
        existing_move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'journal_id': self.sales_journal.id,
            'stripe_invoice_id': 'in_BULK_ALREADY_IMPORTED',
        })
        service = MagicMock()
        service.get_invoices.return_value = [{
            'id': 'in_BULK_ALREADY_IMPORTED',
            'customer': 'cus_SHOULD_NOT_FETCH',
            'created': int(datetime(2024, 1, 2).timestamp()),
        }]
        service.get_credit_notes.return_value = []
        service.get_customer.side_effect = AssertionError('duplicate customer fetched')
        service.get_invoice_lines.side_effect = AssertionError('duplicate lines fetched')

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ), patch.object(
            type(self.stripe_account),
            '_get_existing_move',
            side_effect=AssertionError('per-object duplicate lookup used'),
        ):
            run = self.stripe_account._fetch_invoices()

        self.assertEqual(run.state, 'done')
        self.assertEqual(run.skipped_count, 1)
        line = run.line_ids.filtered(
            lambda run_line: run_line.stripe_object_id == 'in_BULK_ALREADY_IMPORTED'
        )
        self.assertEqual(len(line), 1)
        self.assertEqual(line.state, 'skipped')
        self.assertEqual(line.move_id, existing_move)
        self.assertEqual(existing_move.stripe_account_id, self.stripe_account)
        service.get_customer.assert_not_called()
        service.get_invoice_lines.assert_not_called()

    def test_fetch_invoices_skips_bulk_existing_lookup_when_out_of_time(self):
        service = MagicMock()
        service.get_invoices.return_value = [{
            'id': 'in_OUT_OF_TIME',
            'customer': 'cus_OUT_OF_TIME',
            'created': int(datetime(2024, 1, 2).timestamp()),
        }]
        service.get_credit_notes.return_value = []

        def fake_commit(_account, processed=0, remaining=None):
            return remaining != 1

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ), patch.object(
            type(self.stripe_account),
            '_commit_import_progress',
            autospec=True,
            side_effect=fake_commit,
        ), patch.object(
            type(self.stripe_account),
            '_get_existing_moves_by_stripe_id',
            autospec=True,
            side_effect=AssertionError('bulk duplicate lookup used after timeout'),
        ):
            run = self.stripe_account._fetch_invoices()

        self.assertEqual(run.state, 'failed')
        service.get_customer.assert_not_called()
        service.get_invoice_lines.assert_not_called()

    def test_fetch_invoices_updates_bulk_map_for_created_moves(self):
        stripe_invoice = {
            'id': 'in_DUPLICATE_IN_SAME_BATCH',
            'customer': 'cus_DUPLICATE_IN_SAME_BATCH',
            'status': 'paid',
            'created': int(datetime(2024, 1, 2).timestamp()),
            'currency': 'eur',
        }
        service = MagicMock()
        service.get_invoices.return_value = [stripe_invoice, dict(stripe_invoice)]
        service.get_credit_notes.return_value = []
        service.get_customer.return_value = {
            'name': 'Same Batch Duplicate',
            'email': '',
            'phone': '',
        }
        service.get_invoice_lines.return_value = [{
            'amount': 1000,
            'description': 'Same batch line',
            'quantity': 1,
            'pricing': {'price_details': {'product': 'prod_SAME_BATCH'}},
        }]

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ):
            run = self.stripe_account._fetch_invoices()

        moves = self.env['account.move'].search([
            ('stripe_invoice_id', '=', 'in_DUPLICATE_IN_SAME_BATCH'),
        ])
        self.assertEqual(len(moves), 1)
        self.assertEqual(run.state, 'done')
        self.assertEqual(run.skipped_count, 1)
        self.assertEqual(len(run.line_ids.filtered(lambda line: line.state == 'done')), 1)
        self.assertEqual(len(run.line_ids.filtered(lambda line: line.state == 'skipped')), 1)
        service.get_customer.assert_called_once_with('cus_DUPLICATE_IN_SAME_BATCH')
        service.get_invoice_lines.assert_called_once_with('in_DUPLICATE_IN_SAME_BATCH')

    def test_process_batch_item_converts_duplicate_constraint_race_to_skip(self):
        existing_move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'journal_id': self.sales_journal.id,
            'stripe_invoice_id': 'in_DUPLICATE_RACE',
        })
        run = self.env['stripe.import.run'].create({
            'stripe_account_id': self.stripe_account.id,
            'state': 'running',
            'message': 'Running',
        })
        existing_moves_by_stripe_id = {}
        stripe_invoice = {
            'id': 'in_DUPLICATE_RACE',
            'customer': 'cus_DUPLICATE_RACE',
            'created': int(datetime(2024, 1, 2).timestamp()),
        }

        with patch.object(
            type(self.stripe_account),
            '_prefetch_stripe_data',
            autospec=True,
            return_value={'stripe_lines': []},
        ), patch.object(
            type(self.stripe_account),
            '_process_stripe_object',
            autospec=True,
            side_effect=ValidationError('This Stripe object has already been imported.'),
        ):
            state, failure, keep_going = self.stripe_account._process_batch_item(
                run,
                stripe_invoice,
                MagicMock(),
                'out_invoice',
                'invoice',
                remaining_after=0,
                existing_moves_by_stripe_id=existing_moves_by_stripe_id,
            )

        self.assertEqual(state, 'skipped')
        self.assertFalse(failure)
        self.assertTrue(keep_going)
        self.assertEqual(existing_moves_by_stripe_id['in_DUPLICATE_RACE'], existing_move)
        line = run.line_ids.filtered(lambda run_line: run_line.stripe_object_id == 'in_DUPLICATE_RACE')
        self.assertEqual(len(line), 1)
        self.assertEqual(line.state, 'skipped')
        self.assertEqual(line.move_id, existing_move)

    def test_fetch_floor_applies_lookback(self):
        self.stripe_account.last_fetch_at = fields.Datetime.to_datetime('2024-04-01 00:00:00')
        self.stripe_account.fetch_lookback_days = 30
        floor = self.stripe_account._get_invoice_fetch_floor()
        self.assertEqual(floor, fields.Datetime.to_datetime('2024-03-02 00:00:00'))

    def test_fetch_floor_respects_invoice_cutoff(self):
        self.stripe_account.last_fetch_at = fields.Datetime.to_datetime('2024-04-01 00:00:00')
        self.stripe_account.fetch_lookback_days = 30
        self.stripe_account.invoice_cutoff_date = fields.Date.to_date('2024-06-01')
        floor = self.stripe_account._get_invoice_fetch_floor()
        self.assertEqual(floor, datetime(2024, 6, 1))

    def test_fetch_floor_first_run_with_cutoff(self):
        self.stripe_account.last_fetch_at = False
        self.stripe_account.invoice_cutoff_date = fields.Date.to_date('2024-01-01')
        floor = self.stripe_account._get_invoice_fetch_floor()
        self.assertEqual(floor, datetime(2024, 1, 1))

    def test_fetch_floor_first_run_no_cutoff(self):
        self.stripe_account.last_fetch_at = False
        self.stripe_account.invoice_cutoff_date = False
        self.assertFalse(self.stripe_account._get_invoice_fetch_floor())

    def test_credit_note_fetch_floor_uses_last_fetch_after_cutoff(self):
        self.stripe_account.last_fetch_at = fields.Datetime.to_datetime('2024-04-01 00:00:00')
        self.stripe_account.invoice_cutoff_date = fields.Date.to_date('2024-02-01')
        floor = self.stripe_account._get_credit_note_fetch_floor()
        self.assertEqual(floor, fields.Datetime.to_datetime('2024-04-01 00:00:00'))

    def test_fetch_invoices_uses_lookback_floor(self):
        self.stripe_account.last_fetch_at = fields.Datetime.to_datetime('2024-04-01 00:00:00')
        self.stripe_account.fetch_lookback_days = 30
        service = MagicMock()
        service.get_invoices.return_value = []
        service.get_credit_notes.return_value = []

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ):
            self.stripe_account._fetch_invoices()

        kwargs = service.get_invoices.call_args.kwargs
        self.assertEqual(
            kwargs.get('created_after'),
            fields.Datetime.to_datetime('2024-03-02 00:00:00'),
        )

    def test_fetch_invoices_does_not_advance_watermark_on_failure(self):
        previous_fetch_at = fields.Datetime.to_datetime('2024-01-01 12:00:00')
        self.stripe_account.last_fetch_at = previous_fetch_at
        service = MagicMock()
        service.get_invoices.return_value = [{
            'id': 'in_FAILING_WATERMARK',
            'customer': None,
            'created': int(datetime(2024, 1, 2).timestamp()),
        }]
        service.get_credit_notes.return_value = []

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ):
            run = self.stripe_account._fetch_invoices()

        self.assertEqual(run.state, 'failed')
        self.assertEqual(self.stripe_account.last_fetch_at, previous_fetch_at)
        failed_line = run.line_ids.filtered(
            lambda line: line.stripe_object_id == 'in_FAILING_WATERMARK'
        )
        self.assertEqual(failed_line.state, 'failed')

    def test_fetch_invoices_advances_watermark_on_success(self):
        self.stripe_account.last_fetch_at = False
        service = MagicMock()
        service.get_invoices.return_value = []
        service.get_credit_notes.return_value = []

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ):
            run = self.stripe_account._fetch_invoices()

        self.assertEqual(run.state, 'done')
        self.assertTrue(self.stripe_account.last_fetch_at)

    def test_fetch_invoices_reaps_stale_running_runs(self):
        stale = self.env['stripe.import.run'].create({
            'stripe_account_id': self.stripe_account.id,
            'state': 'running',
            'message': 'Pretend this was abandoned by a previous worker.',
        })
        service = MagicMock()
        service.get_invoices.return_value = []
        service.get_credit_notes.return_value = []

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ):
            self.stripe_account._fetch_invoices()

        # The reaper commits via a separate cursor — invalidate to refetch.
        stale.invalidate_recordset()
        self.assertEqual(stale.state, 'failed')

    # ── _cron_fetch_all ─────────────────────────────────────────────────

    def test_cron_queue_all_fetches_queues_active_accounts_only(self):
        other_company = self.env['res.company'].create({'name': 'Cron Other'})
        other_journal = self._create_sales_journal(
            company=other_company,
            name='Cron Other Active Sales',
        )
        inactive_journal = self._create_sales_journal(
            company=other_company,
            name='Cron Other Inactive Sales',
        )
        active_account = self.env['stripe.account'].create({
            'name': 'Cron Active',
            'api_key': 'sk_test_active',
            'stripe_account_identifier': 'acct_CRONACTIVE',
            'company_id': other_company.id,
            'sales_journal_id': other_journal.id,
        })
        inactive_account = self.env['stripe.account'].create({
            'name': 'Cron Inactive',
            'api_key': 'sk_test_inactive',
            'stripe_account_identifier': 'acct_CRONINACTIVE',
            'company_id': other_company.id,
            'sales_journal_id': inactive_journal.id,
            'active': False,
        })

        with patch.object(type(self.env['ir.cron']), '_commit_progress'), patch.object(
            type(self.stripe_account), '_trigger_fetch_worker'
        ) as trigger:
            self.env['stripe.account'].with_user(SUPERUSER_ID)._cron_queue_all_fetches()

        self.assertTrue(self.stripe_account.fetch_requested_at)
        self.assertEqual(self.stripe_account.fetch_request_source, 'scheduled')
        self.assertTrue(active_account.fetch_requested_at)
        self.assertFalse(inactive_account.fetch_requested_at)
        trigger.assert_called_once()

    def test_cron_process_fetch_queue_processes_one_account_at_a_time(self):
        other_journal = self._create_sales_journal(name='Cron Second Sales')
        other_account = self.env['stripe.account'].create({
            'name': 'Cron Second',
            'api_key': 'sk_test_second',
            'stripe_account_identifier': 'acct_CRONSECOND',
            'company_id': self.company.id,
            'sales_journal_id': other_journal.id,
        })
        self.stripe_account.write({
            'fetch_requested_at': fields.Datetime.to_datetime('2024-01-01 00:00:00'),
            'fetch_request_source': 'scheduled',
        })
        other_account.write({
            'fetch_requested_at': fields.Datetime.to_datetime('2024-01-02 00:00:00'),
            'fetch_request_source': 'scheduled',
        })

        processed = []

        def fake_fetch(account):
            processed.append(account.id)

        with patch.object(type(self.env['ir.cron']), '_commit_progress'), patch.object(
            type(self.stripe_account), '_fetch_invoices', autospec=True, side_effect=fake_fetch
        ), patch.object(type(self.stripe_account), '_trigger_fetch_worker') as trigger:
            self.env['stripe.account']._cron_process_fetch_queue()

        self.assertEqual(processed, [self.stripe_account.id])
        self.assertFalse(self.stripe_account.fetch_requested_at)
        self.assertFalse(self.stripe_account.fetch_started_at)
        self.assertTrue(other_account.fetch_requested_at)
        self.assertFalse(other_account.fetch_started_at)
        trigger.assert_called_once()

    def test_cron_process_fetch_queue_keeps_account_queued_when_fetch_stops_early(self):
        other_journal = self._create_sales_journal(name='Cron Early Stop Sales')
        other_account = self.env['stripe.account'].create({
            'name': 'Cron Early Stop Second',
            'api_key': 'sk_test_early_stop_second',
            'stripe_account_identifier': 'acct_CRONEARLYSTOP',
            'company_id': self.company.id,
            'sales_journal_id': other_journal.id,
        })
        requested_at = fields.Datetime.to_datetime('2024-01-01 00:00:00')
        other_requested_at = fields.Datetime.to_datetime('2024-01-02 00:00:00')
        self.stripe_account.write({
            'fetch_requested_at': requested_at,
            'fetch_request_source': 'scheduled',
        })
        other_account.write({
            'fetch_requested_at': other_requested_at,
            'fetch_request_source': 'scheduled',
        })

        processed = []

        def fake_fetch(account):
            processed.append(account.id)
            # Simulate _fetch_invoices() stopping early: it re-queues the account
            # in the same second as the worker claim.
            account._queue_fetch(source='scheduled')

        with patch.object(fields.Datetime, 'now', return_value=requested_at), patch.object(
            type(self.env['ir.cron']), '_commit_progress'
        ), patch.object(
            type(self.stripe_account), '_fetch_invoices', autospec=True, side_effect=fake_fetch
        ), patch.object(type(self.stripe_account), '_trigger_fetch_worker') as trigger:
            self.env['stripe.account']._cron_process_fetch_queue()

        self.assertEqual(processed, [self.stripe_account.id])
        self.assertEqual(
            self.stripe_account.fetch_requested_at,
            requested_at + timedelta(seconds=1),
        )
        self.assertFalse(self.stripe_account.fetch_started_at)
        self.assertEqual(other_account.fetch_requested_at, other_requested_at)
        self.assertFalse(other_account.fetch_started_at)
        trigger.assert_called_once()

    def test_cron_process_fetch_queue_second_run_processes_second_account(self):
        other_journal = self._create_sales_journal(name='Cron Second Run Sales')
        other_account = self.env['stripe.account'].create({
            'name': 'Cron Second Run',
            'api_key': 'sk_test_second_run',
            'stripe_account_identifier': 'acct_CRONSECONDRUN',
            'company_id': self.company.id,
            'sales_journal_id': other_journal.id,
        })
        self.stripe_account.write({
            'fetch_requested_at': fields.Datetime.to_datetime('2024-01-01 00:00:00'),
            'fetch_request_source': 'scheduled',
        })
        other_account.write({
            'fetch_requested_at': fields.Datetime.to_datetime('2024-01-02 00:00:00'),
            'fetch_request_source': 'scheduled',
        })

        processed = []

        def fake_fetch(account):
            processed.append(account.id)

        with patch.object(type(self.env['ir.cron']), '_commit_progress'), patch.object(
            type(self.stripe_account), '_fetch_invoices', autospec=True, side_effect=fake_fetch
        ), patch.object(type(self.stripe_account), '_trigger_fetch_worker'):
            self.env['stripe.account']._cron_process_fetch_queue()
            self.env['stripe.account']._cron_process_fetch_queue()

        self.assertEqual(processed, [self.stripe_account.id, other_account.id])
        self.assertFalse(self.stripe_account.fetch_requested_at)
        self.assertFalse(other_account.fetch_requested_at)

    def test_cron_process_fetch_queue_clears_failed_account_and_keeps_others(self):
        other_journal = self._create_sales_journal(name='Cron Failure Survivor Sales')
        other_account = self.env['stripe.account'].create({
            'name': 'Cron Survives Failure',
            'api_key': 'sk_test_survives',
            'stripe_account_identifier': 'acct_CRONSURVIVES',
            'company_id': self.company.id,
            'sales_journal_id': other_journal.id,
        })
        self.stripe_account.write({
            'fetch_requested_at': fields.Datetime.to_datetime('2024-01-01 00:00:00'),
            'fetch_request_source': 'scheduled',
        })
        other_account.write({
            'fetch_requested_at': fields.Datetime.to_datetime('2024-01-02 00:00:00'),
            'fetch_request_source': 'scheduled',
        })

        def fake_fetch(account):
            if account.id == self.stripe_account.id:
                raise RuntimeError('boom')

        with patch.object(type(self.env['ir.cron']), '_commit_progress'), patch.object(
            type(self.stripe_account), '_fetch_invoices', autospec=True, side_effect=fake_fetch
        ), patch.object(type(self.stripe_account), '_trigger_fetch_worker') as trigger:
            self.env['stripe.account']._cron_process_fetch_queue()

        self.assertFalse(self.stripe_account.fetch_requested_at)
        self.assertFalse(self.stripe_account.fetch_started_at)
        self.assertTrue(other_account.fetch_requested_at)
        trigger.assert_called_once()

    def test_requeue_stale_fetches_keeps_request_and_clears_started_at(self):
        self.stripe_account.write({
            'fetch_requested_at': fields.Datetime.to_datetime('2024-01-01 00:00:00'),
            'fetch_started_at': fields.Datetime.to_datetime('2024-01-01 00:05:00'),
            'fetch_request_source': 'scheduled',
        })

        with patch.object(
            type(self.stripe_account),
            '_fetch_queue_stale_cutoff',
            return_value=fields.Datetime.to_datetime('2024-01-01 00:10:00'),
        ):
            stale = self.env['stripe.account']._requeue_stale_fetches()

        self.assertEqual(stale.ids, self.stripe_account.ids)
        self.assertTrue(self.stripe_account.fetch_requested_at)
        self.assertFalse(self.stripe_account.fetch_started_at)

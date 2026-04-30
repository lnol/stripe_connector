from datetime import datetime, timezone
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
        cls.sales_journal = cls.env['account.journal'].search([
            ('type', '=', 'sale'),
            ('company_id', '=', cls.company.id),
        ], limit=1)
        cls.revenue_account = cls.env['account.account'].search([
            ('account_type', '=', 'income'),
            ('company_ids', 'in', [cls.company.id]),
        ], limit=1)
        cls.stripe_account = cls.env['stripe.account'].create({
            'name': 'Test Stripe',
            'api_key': 'sk_test_dummy',
            'company_id': cls.company.id,
            'sales_journal_id': cls.sales_journal.id,
            'default_revenue_account_id': cls.revenue_account.id,
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
        service.get_customer.assert_called_once_with('cus_NEW123')

    def test_resolve_partner_returns_existing(self):
        existing = self.env['res.partner'].create({
            'name': 'Existing Corp',
            'stripe_customer_id': 'cus_EXIST456',
        })
        service = MagicMock()
        partner = self.stripe_account._resolve_partner('cus_EXIST456', service)
        self.assertEqual(partner.id, existing.id)
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

    # ── _get_line_product_id ────────────────────────────────────────────

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

    def test_cron_fetch_all_runs_as_root_via_sudo(self):
        service = MagicMock()
        service.get_customer.return_value = {'name': 'Cron Cust', 'email': '', 'phone': ''}
        service.get_invoice_lines.return_value = [{
            'amount': 1000,
            'description': 'Cron line',
            'quantity': 1,
            'pricing': {'price_details': {'product': 'prod_CRON'}},
        }]
        service.get_pdf.return_value = b''
        service.get_invoices.return_value = [{
            'id': 'in_CRON_AS_ROOT',
            'customer': 'cus_CRONROOT',
            'status': 'paid',
            'created': 1700000000,
            'currency': 'eur',
            'invoice_pdf': None,
        }]
        service.get_credit_notes.return_value = []

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', return_value=service
        ):
            self.env['stripe.account'].with_user(SUPERUSER_ID)._cron_fetch_all()

        move = self.env['account.move'].search([('stripe_invoice_id', '=', 'in_CRON_AS_ROOT')])
        self.assertEqual(len(move), 1)

    def test_cron_fetch_all_isolates_account_failures(self):
        other_company = self.env['res.company'].create({'name': 'Cron Other'})
        other_journal = self.env['account.journal'].create({
            'name': 'Cron Other Sales',
            'code': 'CRNOS',
            'type': 'sale',
            'company_id': other_company.id,
        })
        failing_account = self.env['stripe.account'].create({
            'name': 'Cron Failing',
            'api_key': 'sk_test_fail',
            'company_id': other_company.id,
            'sales_journal_id': other_journal.id,
        })

        good_service = MagicMock()
        good_service.get_invoices.return_value = []
        good_service.get_credit_notes.return_value = []

        def get_service(self_account):
            if self_account.id == failing_account.id:
                raise RuntimeError('boom')
            return good_service

        with patch.object(
            type(self.stripe_account), '_get_stripe_service', autospec=True, side_effect=get_service
        ):
            self.env['stripe.account']._cron_fetch_all()

        # Good account still ran to completion
        self.assertTrue(self.stripe_account.last_fetch_at)

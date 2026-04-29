from datetime import datetime
from unittest.mock import MagicMock, patch

from odoo import fields
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

    # ── _resolve_product ────────────────────────────────────────────────

    def test_resolve_product_creates_new(self):
        product = self.stripe_account._resolve_product('prod_NEW001', 'Widget Pro')
        self.assertEqual(product.stripe_product_id, 'prod_NEW001')
        self.assertEqual(product.name, 'Widget Pro')
        self.assertEqual(product.type, 'service')
        self.assertTrue(product.sale_ok)
        self.assertFalse(product.purchase_ok)

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

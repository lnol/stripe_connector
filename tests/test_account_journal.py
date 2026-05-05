from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged('post_install', '-at_install', 'stripe_connector')
class TestAccountJournal(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.sales_journal = cls.env['account.journal'].search([
            ('type', '=', 'sale'),
            ('company_id', '=', cls.company.id),
        ], limit=1)
        # Archive any pre-existing stripe accounts on this journal so the test
        # class owns the only active stripe.account in scope. The class-level
        # savepoint restores them when the class tears down.
        cls.env['stripe.account'].with_context(active_test=False).search([
            ('sales_journal_id', '=', cls.sales_journal.id),
        ]).write({'active': False})
        cls.stripe_account = cls.env['stripe.account'].create({
            'name': 'Test Stripe Journal',
            'api_key': 'sk_test_dummy',
            'stripe_account_identifier': 'acct_TESTJOURNAL',
            'company_id': cls.company.id,
            'sales_journal_id': cls.sales_journal.id,
        })

    def test_stripe_account_id_computed(self):
        self.assertEqual(self.sales_journal.stripe_account_id, self.stripe_account)

    def test_show_stripe_fetch_button_true_when_linked(self):
        self.assertTrue(self.sales_journal.show_stripe_fetch_button)

    def test_show_stripe_fetch_button_false_when_not_linked(self):
        purchase_journal = self.env['account.journal'].search([
            ('type', '=', 'purchase'),
            ('company_id', '=', self.company.id),
        ], limit=1)
        self.assertFalse(purchase_journal.show_stripe_fetch_button)

    def test_show_stripe_fetch_button_false_when_account_archived(self):
        self.stripe_account.active = False
        self.assertFalse(self.sales_journal.show_stripe_fetch_button)
        self.stripe_account.active = True

    def test_fetch_button_queues_only_clicked_journal_account(self):
        other_company = self.env['res.company'].create({'name': 'Stripe Other Company'})
        other_journal = self.env['account.journal'].create({
            'name': 'Stripe Other Sales',
            'code': 'STOS',
            'type': 'sale',
            'company_id': other_company.id,
        })
        other_account = self.env['stripe.account'].create({
            'name': 'Other Stripe Journal',
            'api_key': 'sk_test_other',
            'stripe_account_identifier': 'acct_OTHERJOURNAL',
            'company_id': other_company.id,
            'sales_journal_id': other_journal.id,
        })

        with patch.object(type(self.stripe_account), '_trigger_fetch_worker') as trigger:
            self.sales_journal.action_stripe_fetch_invoices()

        self.assertTrue(self.stripe_account.fetch_requested_at)
        self.assertEqual(self.stripe_account.fetch_request_source, 'manual')
        self.assertFalse(other_account.fetch_requested_at)
        trigger.assert_called_once()

    def test_stripe_account_id_none_for_unlinked_journal(self):
        other_journal = self.env['account.journal'].search([
            ('type', '=', 'purchase'),
            ('company_id', '=', self.company.id),
        ], limit=1)
        self.assertFalse(other_journal.stripe_account_id)

    def test_field_extensions_exist(self):
        partner = self.env['res.partner'].new({'name': 'Test'})
        self.assertIn('stripe_account_identifier', self.env['stripe.account']._fields)
        self.assertIn('stripe_customer_id', self.env['res.partner']._fields)
        self.assertIn('stripe_account_id', self.env['res.partner']._fields)

        self.assertIn('stripe_invoice_id', self.env['account.move']._fields)
        self.assertIn('stripe_account_id', self.env['account.move']._fields)
        self.assertFalse(self.env['account.move']._fields['stripe_invoice_id'].readonly)
        self.assertIn('stripe_product_id', self.env['product.template']._fields)
        self.assertIn('stripe_account_id', self.env['product.template']._fields)

    def test_open_stripe_product_action(self):
        product = self.env['product.template'].create({
            'name': 'Stripe Product',
            'stripe_product_id': 'prod_TEST123',
            'stripe_account_id': self.stripe_account.id,
            'type': 'service',
        })

        action = product.action_open_stripe_product()

        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual(
            action['url'],
            'https://dashboard.stripe.com/acct_TESTJOURNAL/products/prod_TEST123',
        )
        self.assertEqual(action['target'], 'new')

    def test_open_stripe_customer_action(self):
        partner = self.env['res.partner'].new({
            'name': 'Stripe Customer',
            'stripe_customer_id': 'cus_TEST123',
            'stripe_account_id': self.stripe_account,
        })

        action = partner.action_open_stripe_customer()

        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual(
            action['url'],
            'https://dashboard.stripe.com/acct_TESTJOURNAL/customers/cus_TEST123',
        )
        self.assertEqual(action['target'], 'new')

    def test_open_stripe_invoice_action(self):
        move = self.env['account.move'].new({
            'stripe_invoice_id': 'in_TEST123',
            'stripe_account_id': self.stripe_account,
            'stripe_object_type': 'invoice',
        })

        action = move.action_open_stripe_invoice()

        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual(
            action['url'],
            'https://dashboard.stripe.com/acct_TESTJOURNAL/invoices/in_TEST123',
        )
        self.assertEqual(action['target'], 'new')

    def test_open_stripe_credit_note_action(self):
        move = self.env['account.move'].new({
            'stripe_invoice_id': 'cn_TEST123',
            'stripe_account_id': self.stripe_account,
            'stripe_object_type': 'credit_note',
        })

        action = move.action_open_stripe_invoice()

        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual(
            action['url'],
            'https://dashboard.stripe.com/acct_TESTJOURNAL/credit_notes/cn_TEST123',
        )
        self.assertEqual(action['target'], 'new')

    def test_fetch_stripe_invoice_pdf_action_downloads_and_attaches_pdf(self):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'journal_id': self.sales_journal.id,
            'stripe_invoice_id': 'in_FETCHPDF123',
            'stripe_account_id': self.stripe_account.id,
            'stripe_object_type': 'invoice',
        })
        service = MagicMock()
        service.get_invoice.return_value = {
            'id': 'in_FETCHPDF123',
            'invoice_pdf': 'https://stripe.example.com/invoice.pdf',
        }
        service.get_pdf.return_value = b'%PDF-fetch-invoice'

        with patch.object(type(self.stripe_account), '_get_stripe_service', return_value=service):
            action = move.action_fetch_stripe_invoice_pdf()

        attachment = self.env['ir.attachment'].search([
            ('res_model', '=', 'account.move'),
            ('res_id', '=', move.id),
            ('name', '=', 'stripe_invoice_in_FETCHPDF123.pdf'),
        ])
        self.assertEqual(len(attachment), 1)
        self.assertEqual(action, {'type': 'ir.actions.client', 'tag': 'reload'})
        service.get_invoice.assert_called_once_with('in_FETCHPDF123')
        service.get_pdf.assert_called_once_with('https://stripe.example.com/invoice.pdf')

    def test_fetch_stripe_invoice_pdf_action_uses_credit_note_endpoint(self):
        move = self.env['account.move'].create({
            'move_type': 'out_refund',
            'journal_id': self.sales_journal.id,
            'stripe_invoice_id': 'cn_FETCHPDF123',
            'stripe_account_id': self.stripe_account.id,
            'stripe_object_type': 'credit_note',
        })
        service = MagicMock()
        service.get_credit_note.return_value = {
            'id': 'cn_FETCHPDF123',
            'pdf': 'https://stripe.example.com/credit-note.pdf',
        }
        service.get_pdf.return_value = b'%PDF-fetch-credit-note'

        with patch.object(type(self.stripe_account), '_get_stripe_service', return_value=service):
            move.action_fetch_stripe_invoice_pdf()

        attachment = self.env['ir.attachment'].search([
            ('res_model', '=', 'account.move'),
            ('res_id', '=', move.id),
            ('name', '=', 'stripe_pdf_cn_FETCHPDF123.pdf'),
        ])
        self.assertEqual(len(attachment), 1)
        service.get_credit_note.assert_called_once_with('cn_FETCHPDF123')
        service.get_pdf.assert_called_once_with('https://stripe.example.com/credit-note.pdf')

    def test_fetch_stripe_invoice_pdf_action_requires_pdf_url(self):
        move = self.env['account.move'].new({
            'stripe_invoice_id': 'in_NOPDF123',
            'stripe_account_id': self.stripe_account,
            'stripe_object_type': 'invoice',
        })
        service = MagicMock()
        service.get_invoice.return_value = {
            'id': 'in_NOPDF123',
            'invoice_pdf': False,
        }

        with patch.object(type(self.stripe_account), '_get_stripe_service', return_value=service):
            with self.assertRaises(UserError):
                move.action_fetch_stripe_invoice_pdf()

    def test_fetch_stripe_invoice_pdf_server_action_is_bound_to_form_view(self):
        action = self.env.ref('stripe_connector.action_fetch_stripe_invoice_pdf')

        self.assertEqual(action.name, 'Fetch Stripe PDF')
        self.assertEqual(action.binding_model_id.model, 'account.move')
        self.assertEqual(action.binding_view_types, 'form')
        self.assertEqual(action.binding_domain, '[("stripe_invoice_id", "!=", False)]')

    def test_fetch_stripe_invoice_pdf_action_raises_for_unknown_object_type(self):
        move = self.env['account.move'].new({
            'stripe_invoice_id': 'in_UNKNOWN123',
            'stripe_account_id': self.stripe_account,
        })
        with self.assertRaisesRegex(UserError, 'stripe_object_type must be either'):
            move.action_fetch_stripe_invoice_pdf()

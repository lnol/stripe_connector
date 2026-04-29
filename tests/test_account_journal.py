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
        cls.stripe_account = cls.env['stripe.account'].create({
            'name': 'Test Stripe Journal',
            'api_key': 'sk_test_dummy',
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

    def test_stripe_account_id_none_for_unlinked_journal(self):
        other_journal = self.env['account.journal'].search([
            ('type', '=', 'purchase'),
            ('company_id', '=', self.company.id),
        ], limit=1)
        self.assertFalse(other_journal.stripe_account_id)

    def test_field_extensions_exist(self):
        partner = self.env['res.partner'].new({'name': 'Test'})
        self.assertIn('stripe_customer_id', self.env['res.partner']._fields)

        self.assertIn('stripe_invoice_id', self.env['account.move']._fields)
        self.assertIn('stripe_product_id', self.env['product.template']._fields)

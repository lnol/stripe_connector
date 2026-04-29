from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'stripe_connector')
class TestAccessRights(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env['res.company'].create({
            'name': 'Other Stripe Company',
        })
        cls.sales_journal = cls.env['account.journal'].search([
            ('type', '=', 'sale'),
            ('company_id', '=', cls.company.id),
        ], limit=1)
        cls.other_sales_journal = cls.env['account.journal'].create({
            'name': 'Other Stripe Sales',
            'code': 'OSTRP',
            'type': 'sale',
            'company_id': cls.other_company.id,
        })
        cls.stripe_account = cls.env['stripe.account'].create({
            'name': 'Main Company Stripe',
            'api_key': 'sk_test_main',
            'company_id': cls.company.id,
            'sales_journal_id': cls.sales_journal.id,
        })
        cls.other_stripe_account = cls.env['stripe.account'].create({
            'name': 'Other Company Stripe',
            'api_key': 'sk_test_other',
            'company_id': cls.other_company.id,
            'sales_journal_id': cls.other_sales_journal.id,
        })
        cls.stripe_user = cls.env['res.users'].create({
            'name': 'Stripe User',
            'login': 'stripe_user@test.local',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, [cls.company.id])],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('stripe_connector.group_stripe_user').id,
            ])],
        })
        cls.stripe_editor_user = cls.env['res.users'].create({
            'name': 'Stripe Editor User',
            'login': 'stripe_editor_user@test.local',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, [cls.company.id])],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('base.group_partner_manager').id,
                cls.env.ref('product.group_product_manager').id,
                cls.env.ref('account.group_account_invoice').id,
                cls.env.ref('stripe_connector.group_stripe_user').id,
            ])],
        })

    def test_stripe_user_cannot_create_account(self):
        with self.assertRaises(AccessError):
            self.env['stripe.account'].with_user(self.stripe_user).create({
                'name': 'Forbidden Stripe',
                'api_key': 'sk_test_forbidden',
                'company_id': self.company.id,
                'sales_journal_id': self.sales_journal.id,
            })

    def test_stripe_user_only_sees_allowed_company_accounts(self):
        visible_accounts = self.env['stripe.account'].with_user(self.stripe_user).search([])
        self.assertIn(self.stripe_account, visible_accounts)
        self.assertNotIn(self.other_stripe_account, visible_accounts)

    def test_stripe_user_cannot_edit_product_stripe_id(self):
        product = self.env['product.template'].sudo().create({
            'name': 'Stripe Product',
            'stripe_product_id': 'prod_ORIGINAL',
            'type': 'service',
        })
        product = self.env['product.template'].browse(product.id)

        with self.assertRaises(AccessError):
            product.with_user(self.stripe_editor_user).write({
                'stripe_product_id': 'prod_CHANGED',
            })

    def test_stripe_user_cannot_edit_customer_stripe_id(self):
        partner = self.env['res.partner'].sudo().create({
            'name': 'Stripe Customer',
            'stripe_customer_id': 'cus_ORIGINAL',
        })
        partner = self.env['res.partner'].browse(partner.id)

        with self.assertRaises(AccessError):
            partner.with_user(self.stripe_editor_user).write({
                'stripe_customer_id': 'cus_CHANGED',
            })

    def test_stripe_user_cannot_edit_invoice_stripe_id(self):
        move = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'journal_id': self.sales_journal.id,
            'stripe_invoice_id': 'in_ORIGINAL',
        })
        move = self.env['account.move'].browse(move.id)

        with self.assertRaises(AccessError):
            move.with_user(self.stripe_editor_user).write({
                'stripe_invoice_id': 'in_CHANGED',
            })

    def test_stripe_user_cannot_create_records_with_stripe_ids(self):
        with self.assertRaises(AccessError):
            self.env['product.template'].with_user(self.stripe_editor_user).create({
                'name': 'Blocked Stripe Product',
                'stripe_product_id': 'prod_BLOCKED',
                'type': 'service',
            })
        with self.assertRaises(AccessError):
            self.env['res.partner'].with_user(self.stripe_editor_user).create({
                'name': 'Blocked Stripe Customer',
                'stripe_customer_id': 'cus_BLOCKED',
            })
        with self.assertRaises(AccessError):
            self.env['account.move'].with_user(self.stripe_editor_user).create({
                'move_type': 'out_invoice',
                'journal_id': self.sales_journal.id,
                'stripe_invoice_id': 'in_BLOCKED',
            })

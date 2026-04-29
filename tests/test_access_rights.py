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

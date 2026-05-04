from odoo import fields, models


class ResPartner(models.Model):
    _name = 'res.partner'
    _inherit = ['res.partner', 'stripe.linked.mixin']

    _stripe_id_field = 'stripe_customer_id'
    _stripe_account_field = 'stripe_account_id'

    stripe_customer_id = fields.Char(
        string='Stripe Customer ID',
        index=True,
        copy=False,
        help='Stripe customer identifier linked to this contact.',
    )
    stripe_account_id = fields.Many2one(
        comodel_name='stripe.account',
        string='Stripe Account',
        index=True,
        copy=False,
        ondelete='restrict',
        help='Stripe account configuration that imported this customer.',
    )

    def action_open_stripe_customer(self):
        return self._stripe_dashboard_url('customers')

from odoo import fields, models


class ResPartner(models.Model):
    _name = 'res.partner'
    _inherit = ['res.partner', 'stripe.linked.mixin']

    _stripe_id_field = 'stripe_customer_id'

    stripe_customer_id = fields.Char(
        string='Stripe Customer ID',
        index=True,
        copy=False,
        help='Stripe customer identifier linked to this contact.',
    )

    def action_open_stripe_customer(self):
        return self._stripe_dashboard_url('customers')

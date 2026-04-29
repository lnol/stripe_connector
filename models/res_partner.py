from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    stripe_customer_id = fields.Char(
        string='Stripe Customer ID',
        index=True,
        copy=False,
        help='Stripe customer identifier linked to this contact.',
    )

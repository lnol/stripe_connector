from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    stripe_invoice_id = fields.Char(
        string='Stripe Invoice ID',
        index=True,
        copy=False,
        readonly=True,
    )

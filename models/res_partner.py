from urllib.parse import quote

from odoo import _, fields, models
from odoo.exceptions import UserError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    stripe_customer_id = fields.Char(
        string='Stripe Customer ID',
        index=True,
        copy=False,
        help='Stripe customer identifier linked to this contact.',
    )

    def action_open_stripe_customer(self):
        self.ensure_one()
        stripe_customer_id = (self.stripe_customer_id or '').strip()
        if not stripe_customer_id:
            raise UserError(_('This contact is not linked to a Stripe customer.'))

        stripe_customer_id = quote(stripe_customer_id, safe='')
        return {
            'type': 'ir.actions.act_url',
            'url': f'https://dashboard.stripe.com/customers/{stripe_customer_id}',
            'target': 'new',
        }

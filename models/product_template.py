from urllib.parse import quote

from odoo import _, fields, models
from odoo.exceptions import UserError


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    stripe_product_id = fields.Char(
        string='Stripe Product ID',
        index=True,
        copy=False,
        help='Stripe product identifier linked to this product.',
    )

    def action_open_stripe_product(self):
        self.ensure_one()
        stripe_product_id = (self.stripe_product_id or '').strip()
        if not stripe_product_id:
            raise UserError(_('This product is not linked to a Stripe product.'))

        stripe_product_id = quote(stripe_product_id, safe='')
        return {
            'type': 'ir.actions.act_url',
            'url': f'https://dashboard.stripe.com/products/{stripe_product_id}',
            'target': 'new',
        }

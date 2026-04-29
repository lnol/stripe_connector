from odoo import fields, models


class ProductTemplate(models.Model):
    _name = 'product.template'
    _inherit = ['product.template', 'stripe.linked.mixin']

    _stripe_id_field = 'stripe_product_id'

    stripe_product_id = fields.Char(
        string='Stripe Product ID',
        index=True,
        copy=False,
        help='Stripe product identifier linked to this product.',
    )

    def action_open_stripe_product(self):
        return self._stripe_dashboard_url('products')

from odoo import fields, models


class ProductTemplate(models.Model):
    _name = 'product.template'
    _inherit = ['product.template', 'stripe.linked.mixin']

    _stripe_id_field = 'stripe_product_id'
    _stripe_account_field = 'stripe_account_id'

    stripe_product_id = fields.Char(
        string='Stripe Product ID',
        index=True,
        copy=False,
        help='Stripe product identifier linked to this product.',
    )
    stripe_account_id = fields.Many2one(
        comodel_name='stripe.account',
        string='Stripe Account',
        index=True,
        copy=False,
        ondelete='restrict',
        help='Stripe account configuration that imported this product.',
    )

    def action_open_stripe_product(self):
        return self._stripe_dashboard_url('products')

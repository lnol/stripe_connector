from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    stripe_product_id = fields.Char(
        string='Stripe Product ID',
        index=True,
        copy=False,
    )

from urllib.parse import quote

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    stripe_product_id = fields.Char(
        string='Stripe Product ID',
        index=True,
        copy=False,
        help='Stripe product identifier linked to this product.',
    )

    def _check_stripe_product_id_write_access(self, vals):
        if 'stripe_product_id' not in vals:
            return
        if self.env.su or self.env.user.has_group('stripe_connector.group_stripe_admin'):
            return

        new_value = vals.get('stripe_product_id') or False
        if any((product.stripe_product_id or False) != new_value for product in self):
            raise AccessError(_('Only Stripe administrators can edit the Stripe Product ID.'))

    def _check_stripe_product_id_create_access(self, vals_list):
        if self.env.su or self.env.user.has_group('stripe_connector.group_stripe_admin'):
            return
        if any(vals.get('stripe_product_id') for vals in vals_list):
            raise AccessError(_('Only Stripe administrators can edit the Stripe Product ID.'))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_stripe_product_id_create_access(vals_list)
        return super().create(vals_list)

    def write(self, vals):
        self._check_stripe_product_id_write_access(vals)
        return super().write(vals)

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

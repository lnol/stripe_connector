from urllib.parse import quote

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    stripe_customer_id = fields.Char(
        string='Stripe Customer ID',
        index=True,
        copy=False,
        help='Stripe customer identifier linked to this contact.',
    )

    def _check_stripe_customer_id_write_access(self, vals):
        if 'stripe_customer_id' not in vals:
            return
        if self.env.su or self.env.user.has_group('stripe_connector.group_stripe_admin'):
            return

        new_value = vals.get('stripe_customer_id') or False
        if any((partner.stripe_customer_id or False) != new_value for partner in self):
            raise AccessError(_('Only Stripe administrators can edit the Stripe Customer ID.'))

    def _check_stripe_customer_id_create_access(self, vals_list):
        if self.env.su or self.env.user.has_group('stripe_connector.group_stripe_admin'):
            return
        if any(vals.get('stripe_customer_id') for vals in vals_list):
            raise AccessError(_('Only Stripe administrators can edit the Stripe Customer ID.'))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_stripe_customer_id_create_access(vals_list)
        return super().create(vals_list)

    def write(self, vals):
        self._check_stripe_customer_id_write_access(vals)
        return super().write(vals)

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

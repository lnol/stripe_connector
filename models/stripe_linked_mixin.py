from urllib.parse import quote

from odoo import _, api, models
from odoo.exceptions import AccessError, UserError


class StripeLinkedMixin(models.AbstractModel):
    _name = 'stripe.linked.mixin'
    _description = 'Mixin for models linked to Stripe via an indexed Char identifier.'

    # Concrete models set this to the field name carrying the Stripe ID, e.g.
    # ``stripe_customer_id`` on ``res.partner``.
    _stripe_id_field = None

    @api.model_create_multi
    def create(self, vals_list):
        self._check_stripe_id_create_access(vals_list)
        return super().create(vals_list)

    def write(self, vals):
        self._check_stripe_id_write_access(vals)
        return super().write(vals)

    def _check_stripe_id_write_access(self, vals):
        field = self._stripe_id_field
        if not field or field not in vals:
            return
        if self.env.su or self.env.user.has_group('stripe_connector.group_stripe_admin'):
            return
        new_value = vals.get(field) or False
        if any((rec[field] or False) != new_value for rec in self):
            raise AccessError(_('Only Stripe administrators can edit the Stripe ID.'))

    def _check_stripe_id_create_access(self, vals_list):
        field = self._stripe_id_field
        if not field:
            return
        if self.env.su or self.env.user.has_group('stripe_connector.group_stripe_admin'):
            return
        if any(vals.get(field) for vals in vals_list):
            raise AccessError(_('Only Stripe administrators can edit the Stripe ID.'))

    def _stripe_dashboard_url(self, path):
        self.ensure_one()
        stripe_id = (self[self._stripe_id_field] or '').strip()
        if not stripe_id:
            raise UserError(_('This record is not linked to a Stripe object.'))
        return {
            'type': 'ir.actions.act_url',
            'url': f'https://dashboard.stripe.com/{path}/{quote(stripe_id, safe="")}',
            'target': 'new',
        }

from urllib.parse import quote

from odoo import _, api, models
from odoo.exceptions import AccessError, UserError


class StripeLinkedMixin(models.AbstractModel):
    _name = 'stripe.linked.mixin'
    _description = 'Mixin for models linked to Stripe via an indexed Char identifier.'

    # Concrete models set this to the field name carrying the Stripe ID, e.g.
    # ``stripe_customer_id`` on ``res.partner``.
    _stripe_id_field = None
    _stripe_account_field = None

    @api.model_create_multi
    def create(self, vals_list):
        self._check_stripe_id_create_access(vals_list)
        return super().create(vals_list)

    def write(self, vals):
        self._check_stripe_id_write_access(vals)
        return super().write(vals)

    def _normalize_stripe_link_value(self, field_name, value):
        field = self._fields[field_name]
        if field.type == 'many2one':
            if isinstance(value, models.BaseModel):
                return value.id or False
            return value or False
        return value or False

    def _check_stripe_id_write_access(self, vals):
        fields_to_check = [
            field for field in (self._stripe_id_field, self._stripe_account_field)
            if field and field in vals
        ]
        if not fields_to_check:
            return
        if self.env.su or self.env.user.has_group('stripe_connector.group_stripe_admin'):
            return
        for field in fields_to_check:
            new_value = self._normalize_stripe_link_value(field, vals.get(field))
            if any(
                self._normalize_stripe_link_value(field, rec[field]) != new_value
                for rec in self
            ):
                raise AccessError(_('Only Stripe administrators can edit Stripe links.'))

    def _check_stripe_id_create_access(self, vals_list):
        fields_to_check = [
            field for field in (self._stripe_id_field, self._stripe_account_field)
            if field
        ]
        if not fields_to_check:
            return
        if self.env.su or self.env.user.has_group('stripe_connector.group_stripe_admin'):
            return
        if any(vals.get(field) for vals in vals_list for field in fields_to_check):
            raise AccessError(_('Only Stripe administrators can edit Stripe links.'))

    def _get_stripe_dashboard_account_identifier(self):
        self.ensure_one()
        field = self._stripe_account_field
        if not field or field not in self._fields:
            return ''
        stripe_account = self[field]
        if not stripe_account:
            return ''
        return (stripe_account.stripe_account_identifier or '').strip()

    def _stripe_dashboard_url(self, path):
        self.ensure_one()
        stripe_id = (self[self._stripe_id_field] or '').strip()
        if not stripe_id:
            raise UserError(_('This record is not linked to a Stripe object.'))
        account_identifier = self._get_stripe_dashboard_account_identifier()
        path_parts = []
        if account_identifier:
            path_parts.append(quote(account_identifier, safe=''))
        path_parts.extend([path, quote(stripe_id, safe='')])
        return {
            'type': 'ir.actions.act_url',
            'url': 'https://dashboard.stripe.com/%s' % '/'.join(path_parts),
            'target': 'new',
        }

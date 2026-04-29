from urllib.parse import quote

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class AccountMove(models.Model):
    _inherit = 'account.move'

    _stripe_invoice_id_unique = models.Constraint(
        'UNIQUE(stripe_invoice_id)',
        'This Stripe object has already been imported.',
    )

    stripe_invoice_id = fields.Char(
        string='Stripe Object ID',
        index=True,
        copy=False,
        help='Stripe invoice or credit note identifier imported into this move.',
    )
    stripe_object_type = fields.Selection(
        selection=[
            ('invoice', 'Invoice'),
            ('credit_note', 'Credit Note'),
        ],
        string='Stripe Object Type',
        copy=False,
        readonly=True,
        help='Type of Stripe object imported into this move.',
    )

    def _check_stripe_invoice_id_write_access(self, vals):
        if 'stripe_invoice_id' not in vals:
            return
        if self.env.su or self.env.user.has_group('stripe_connector.group_stripe_admin'):
            return

        new_value = vals.get('stripe_invoice_id') or False
        if any((move.stripe_invoice_id or False) != new_value for move in self):
            raise AccessError(_('Only Stripe administrators can edit the Stripe Object ID.'))

    def _check_stripe_invoice_id_create_access(self, vals_list):
        if self.env.su or self.env.user.has_group('stripe_connector.group_stripe_admin'):
            return
        if any(vals.get('stripe_invoice_id') for vals in vals_list):
            raise AccessError(_('Only Stripe administrators can edit the Stripe Object ID.'))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_stripe_invoice_id_create_access(vals_list)
        return super().create(vals_list)

    def write(self, vals):
        self._check_stripe_invoice_id_write_access(vals)
        return super().write(vals)

    def action_open_stripe_invoice(self):
        self.ensure_one()
        stripe_object_id = (self.stripe_invoice_id or '').strip()
        if not stripe_object_id:
            raise UserError(_('This invoice is not linked to a Stripe object.'))

        path = 'credit_notes' if self.stripe_object_type == 'credit_note' else 'invoices'
        stripe_object_id = quote(stripe_object_id, safe='')
        return {
            'type': 'ir.actions.act_url',
            'url': f'https://dashboard.stripe.com/{path}/{stripe_object_id}',
            'target': 'new',
        }

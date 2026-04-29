from urllib.parse import quote

from odoo import _, fields, models
from odoo.exceptions import UserError


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
        readonly=True,
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

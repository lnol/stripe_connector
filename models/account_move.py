from odoo import fields, models


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

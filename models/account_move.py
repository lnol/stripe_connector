from odoo import fields, models


class AccountMove(models.Model):
    _name = 'account.move'
    _inherit = ['account.move', 'stripe.linked.mixin']

    _stripe_id_field = 'stripe_invoice_id'
    _stripe_account_field = 'stripe_account_id'

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
    stripe_account_id = fields.Many2one(
        comodel_name='stripe.account',
        string='Stripe Account',
        index=True,
        copy=False,
        ondelete='restrict',
        help='Stripe account configuration that imported this move.',
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
        path = 'credit_notes' if self.stripe_object_type == 'credit_note' else 'invoices'
        return self._stripe_dashboard_url(path)

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    stripe_account_id = fields.Many2one(
        comodel_name='stripe.account',
        string='Stripe Account',
        compute='_compute_stripe_account_id',
    )
    show_stripe_fetch_button = fields.Boolean(
        string='Show Stripe Fetch Button',
        compute='_compute_show_stripe_fetch_button',
    )

    @api.depends('type', 'company_id')
    def _compute_stripe_account_id(self):
        for journal in self:
            journal.stripe_account_id = self.env['stripe.account'].search([
                ('sales_journal_id', '=', journal.id),
                ('active', '=', True),
                ('company_id', '=', journal.company_id.id),
            ], limit=1)

    @api.depends('stripe_account_id')
    def _compute_show_stripe_fetch_button(self):
        for journal in self:
            journal.show_stripe_fetch_button = bool(journal.stripe_account_id)

    def action_stripe_fetch_invoices(self):
        self.ensure_one()
        if not self.env.user.has_group('stripe_connector.group_stripe_admin'):
            raise AccessError(_('Only Stripe administrators can fetch Stripe invoices.'))
        if not self.stripe_account_id:
            return
        self.stripe_account_id._trigger_fetch()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Stripe Fetch Started'),
                'message': _(
                    'Invoices are being imported in the background. '
                    'Check the import history on the Stripe account for results.'
                ),
                'type': 'info',
            },
        }

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
        if not self:
            return
        accounts = self.env['stripe.account'].search([
            ('sales_journal_id', 'in', self.ids),
            ('active', '=', True),
        ])
        # ``stripe.account`` enforces ``_check_company_auto`` so the journal's
        # company already matches the account's company — no second filter needed.
        by_journal = {a.sales_journal_id.id: a for a in accounts}
        for journal in self:
            journal.stripe_account_id = by_journal.get(journal.id, False)

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

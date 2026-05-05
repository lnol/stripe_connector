from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError


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

    def _get_stripe_pdf_fetch_spec(self):
        self.ensure_one()
        if self.stripe_object_type == 'credit_note':
            return 'credit note', 'pdf', 'get_credit_note'
        return 'invoice', 'invoice_pdf', 'get_invoice'

    def action_fetch_stripe_invoice_pdf(self):
        self.ensure_one()
        if not (
            self.env.su
            or self.env.user.has_group('stripe_connector.group_stripe_user')
        ):
            raise AccessError(_('Only Stripe users can fetch Stripe PDFs.'))
        if not self.stripe_invoice_id:
            raise UserError(_('This document is not linked to a Stripe object.'))
        if not self.stripe_account_id:
            raise UserError(_('This document is not linked to a Stripe account.'))

        stripe_account = self.stripe_account_id.sudo()
        stripe_object_label, pdf_field, service_method_name = self._get_stripe_pdf_fetch_spec()
        service = stripe_account._get_stripe_service()
        stripe_obj = getattr(service, service_method_name)(self.stripe_invoice_id)
        pdf_url = stripe_obj.get(pdf_field)
        if not pdf_url:
            raise UserError(
                _('Stripe %(object_type)s %(stripe_id)s does not provide a PDF URL.') % {
                    'object_type': stripe_object_label,
                    'stripe_id': self.stripe_invoice_id,
                }
            )
        try:
            pdf_bytes = service.get_pdf(pdf_url)
        except Exception as error:
            raise UserError(_('Could not download the Stripe PDF: %s') % error) from error

        stripe_account._attach_pdf(
            self,
            stripe_obj,
            pdf_field=pdf_field,
            pdf_bytes=pdf_bytes,
        )
        return {'type': 'ir.actions.client', 'tag': 'reload'}

import base64
import logging
from datetime import datetime, timezone

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StripeAccount(models.Model):
    _name = 'stripe.account'
    _description = 'Stripe Account Configuration'
    _check_company_auto = True

    name = fields.Char(string='Name', required=True)
    api_key = fields.Char(
        string='API Secret Key',
        required=True,
        groups='stripe_connector.group_stripe_admin',
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
    )
    sales_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string='Sales Journal',
        domain="[('type', '=', 'sale'), ('company_id', '=', company_id)]",
        check_company=True,
        required=True,
    )
    bank_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string='Bank/Cash Journal',
        domain="[('type', 'in', ['bank', 'cash']), ('company_id', '=', company_id)]",
        check_company=True,
    )
    auto_confirm = fields.Boolean(
        string='Auto-Confirm Invoices',
        default=False,
        help='Automatically post (confirm) fetched invoices.',
    )
    active = fields.Boolean(default=True)
    last_fetch_date = fields.Date(
        string='Last Fetch Date',
        readonly=True,
        copy=False,
    )
    default_revenue_account_id = fields.Many2one(
        comodel_name='account.account',
        string='Default Revenue Account',
        domain="[('account_type', 'not in', ("
               "'asset_receivable', 'liability_payable', "
               "'asset_cash', 'liability_credit_card', 'off_balance'"
               ")), ('company_ids', 'in', company_id)]",
        help='Fallback revenue account when the product has no income account set.',
    )

    def _get_stripe_service(self):
        self.ensure_one()
        from ..services.stripe_api import StripeApiService
        return StripeApiService(self.api_key)

    def _resolve_partner(self, stripe_customer_id, service):
        partner = self.env['res.partner'].search(
            [('stripe_customer_id', '=', stripe_customer_id)], limit=1
        )
        if not partner:
            customer_data = service.get_customer(stripe_customer_id)
            partner = self.env['res.partner'].create({
                'name': (
                    customer_data.get('name')
                    or customer_data.get('email')
                    or stripe_customer_id
                ),
                'email': customer_data.get('email') or '',
                'phone': customer_data.get('phone') or '',
                'stripe_customer_id': stripe_customer_id,
                'customer_rank': 1,
            })
        return partner

    def _resolve_product(self, stripe_product_id, product_name):
        product_tmpl = self.env['product.template'].search(
            [('stripe_product_id', '=', stripe_product_id)], limit=1
        )
        if not product_tmpl:
            product_tmpl = self.env['product.template'].create({
                'name': product_name or stripe_product_id,
                'stripe_product_id': stripe_product_id,
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
            })
        return product_tmpl

    def _get_line_product_id(self, line):
        pricing = line.get('pricing') or {}
        price_details = pricing.get('price_details') or {}
        product_id = price_details.get('product')
        if not product_id:
            price = line.get('price') or {}
            product_id = price.get('product')
        return product_id

    def _build_invoice_lines(self, stripe_invoice, service):
        lines = []
        stripe_lines = service.get_invoice_lines(stripe_invoice['id'])
        for line in stripe_lines:
            stripe_product_id = self._get_line_product_id(line)
            product_name = line.get('description') or ''
            product_tmpl = None
            if stripe_product_id:
                product_tmpl = self._resolve_product(stripe_product_id, product_name)

            account_id = False
            if product_tmpl:
                income_account = product_tmpl.property_account_income_id
                if income_account:
                    account_id = income_account.id
            if not account_id and self.default_revenue_account_id:
                account_id = self.default_revenue_account_id.id

            line_vals = {
                'name': line.get('description') or (product_tmpl.name if product_tmpl else '/'),
                'quantity': line.get('quantity') or 1,
                'price_unit': (line.get('amount') or 0) / 100.0,
            }
            if product_tmpl:
                product_product = product_tmpl.product_variant_ids[:1]
                if product_product:
                    line_vals['product_id'] = product_product.id
            if account_id:
                line_vals['account_id'] = account_id

            lines.append((0, 0, line_vals))
        return lines

    def _attach_pdf(self, move, stripe_obj, service, pdf_field='invoice_pdf'):
        pdf_url = stripe_obj.get(pdf_field)
        if not pdf_url:
            return
        try:
            pdf_bytes = service.get_pdf(pdf_url)
            self.env['ir.attachment'].create({
                'name': 'stripe_%s_%s.pdf' % (pdf_field.replace('_pdf', ''), stripe_obj['id']),
                'datas': base64.b64encode(pdf_bytes),
                'mimetype': 'application/pdf',
                'res_model': 'account.move',
                'res_id': move.id,
            })
        except Exception as e:
            _logger.warning(
                'Could not attach Stripe PDF for %s: %s', stripe_obj.get('id'), e
            )

    def _process_stripe_invoice(self, stripe_invoice, service, move_type='out_invoice'):
        stripe_id = stripe_invoice.get('id')
        if not stripe_id:
            return

        existing = self.env['account.move'].search(
            [('stripe_invoice_id', '=', stripe_id)], limit=1
        )
        if existing:
            return

        stripe_customer_id = stripe_invoice.get('customer')
        if not stripe_customer_id:
            _logger.warning('Stripe invoice %s has no customer, skipping.', stripe_id)
            return

        partner = self._resolve_partner(stripe_customer_id, service)

        invoice_date = None
        created_ts = stripe_invoice.get('created')
        if created_ts:
            invoice_date = datetime.fromtimestamp(created_ts, tz=timezone.utc).date()

        invoice_date_due = None
        due_ts = stripe_invoice.get('due_date')
        if due_ts:
            invoice_date_due = datetime.fromtimestamp(due_ts, tz=timezone.utc).date()

        invoice_line_ids = self._build_invoice_lines(stripe_invoice, service)

        move_vals = {
            'move_type': move_type,
            'journal_id': self.sales_journal_id.id,
            'partner_id': partner.id,
            'invoice_date': invoice_date,
            'invoice_date_due': invoice_date_due,
            'stripe_invoice_id': stripe_id,
            'invoice_line_ids': invoice_line_ids,
        }

        move = self.env['account.move'].create(move_vals)

        pdf_field = 'pdf' if move_type == 'out_refund' else 'invoice_pdf'
        self._attach_pdf(move, stripe_invoice, service, pdf_field=pdf_field)

        if self.auto_confirm:
            move.with_context(disable_abnormal_invoice_detection=True).action_post()

    def _fetch_invoices(self):
        self.ensure_one()
        service = self._get_stripe_service()

        stripe_invoices = service.get_invoices(created_after=self.last_fetch_date)
        for inv in stripe_invoices:
            try:
                self._process_stripe_invoice(inv, service, move_type='out_invoice')
            except Exception as e:
                _logger.error(
                    'Error processing Stripe invoice %s: %s', inv.get('id'), e
                )

        stripe_credit_notes = service.get_credit_notes(created_after=self.last_fetch_date)
        for cn in stripe_credit_notes:
            try:
                self._process_stripe_invoice(cn, service, move_type='out_refund')
            except Exception as e:
                _logger.error(
                    'Error processing Stripe credit note %s: %s', cn.get('id'), e
                )

        self.last_fetch_date = fields.Date.today()
        return True

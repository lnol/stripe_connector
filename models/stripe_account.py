import base64
import logging
import re
from datetime import datetime, timedelta, timezone

from psycopg2 import IntegrityError, InterfaceError, OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import config, html_escape

from ..const import legacy_placeholder_stripe_account_identifier
from ..services.stripe_api import StripeApiService

_logger = logging.getLogger(__name__)
_CRON_TIME_BUDGET_MESSAGE = (
    'Cron time budget exhausted; remaining Stripe objects will be retried on the next run.'
)
_DUPLICATE_STRIPE_MOVE_MESSAGE = 'This Stripe object has already been imported.'
_STRIPE_DESCRIPTION_QUANTITY_RE = re.compile(
    r'^\s*\d+(?:[.,]\d+)?\s*(?:x|\u00d7)\s+'
)
_STRIPE_DESCRIPTION_PRICE_RE = re.compile(
    r'\s+\(at\s+'
    r'(?=[^)]*\d)'
    r'(?=[^)]*(?:/|\d[.,]\d|[$]|[A-Z]{3}\b))'
    r'[^)]*\)\s*$',
    re.IGNORECASE,
)


def _is_closed_db_connection_error(error):
    return isinstance(error, InterfaceError) or (
        isinstance(error, OperationalError) and 'closed' in str(error).lower()
    )


def _is_duplicate_stripe_move_error(error):
    if isinstance(error, IntegrityError):
        diag = getattr(error, 'diag', None)
        constraint_name = getattr(diag, 'constraint_name', '') if diag else ''
        return 'stripe_invoice_id' in constraint_name or 'stripe_invoice_id' in str(error)
    return (
        isinstance(error, ValidationError)
        and _DUPLICATE_STRIPE_MOVE_MESSAGE in str(error)
    )


class StripeAccount(models.Model):
    _name = 'stripe.account'
    _description = 'Stripe Account Configuration'
    _order = 'name'
    _check_company_auto = True
    _sales_journal_id_unique = models.Constraint(
        'UNIQUE(sales_journal_id)',
        'Each sales journal can only be linked to one Stripe account.',
    )

    name = fields.Char(
        string='Name',
        required=True,
        help='Display name for this Stripe account configuration.',
    )
    api_key = fields.Char(
        string='API Secret Key',
        required=True,
        groups='stripe_connector.group_stripe_admin',
        help='Stripe secret API key used to fetch invoices, credit notes, and PDFs.',
    )
    stripe_account_identifier = fields.Char(
        string='Stripe Account ID',
        required=True,
        index=True,
        copy=False,
        help='Stripe account identifier used in Dashboard URLs, for example acct_1P4I63KFrsB6EyRC.',
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
        ondelete='restrict',
        help='Company that owns this Stripe account configuration.',
    )
    sales_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string='Sales Journal',
        domain="[('type', '=', 'sale'), ('company_id', '=', company_id)]",
        check_company=True,
        required=True,
        ondelete='restrict',
        help='Sales journal used for imported Stripe invoices and credit notes.',
    )
    bank_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string='Bank/Cash Journal',
        domain="[('type', 'in', ['bank', 'cash']), ('company_id', '=', company_id)]",
        check_company=True,
        ondelete='set null',
        help='Optional bank or cash journal related to this Stripe account.',
    )
    auto_confirm = fields.Boolean(
        string='Auto-Confirm Invoices',
        default=False,
        help='Automatically post imported invoices and credit notes.',
    )
    invoice_cutoff_date = fields.Date(
        string='Cut-off Date',
        help=(
            'Invoices finalized and credit notes issued before this date are ignored. '
            'Use this to avoid importing historical Stripe objects that were already '
            'handled manually.'
        ),
    )
    active = fields.Boolean(
        string='Active',
        default=True,
        help='Disable this option to archive the Stripe account configuration.',
    )
    last_fetch_at = fields.Datetime(
        string='Last Successful Fetch',
        readonly=True,
        copy=False,
        help='Timestamp of the last fully successful Stripe fetch.',
    )
    fetch_lookback_days = fields.Integer(
        string='Fetch Lookback (days)',
        default=90,
        help=(
            'On every fetch, also re-scan invoices created within this many days. '
            'Catches subscription invoices that were created earlier but only finalized recently. '
            'Already-imported invoices are skipped via deduplication.'
        ),
    )
    fetch_requested_at = fields.Datetime(
        string='Fetch Requested At',
        readonly=True,
        copy=False,
        help='Timestamp of the pending Stripe fetch request for this account.',
    )
    fetch_started_at = fields.Datetime(
        string='Fetch Started At',
        readonly=True,
        copy=False,
        help='Timestamp of the currently running queued Stripe fetch.',
    )
    fetch_request_source = fields.Selection(
        selection=[
            ('manual', 'Manual'),
            ('scheduled', 'Scheduled'),
        ],
        string='Fetch Request Source',
        readonly=True,
        copy=False,
        help='Origin of the pending Stripe fetch request.',
    )
    default_revenue_account_id = fields.Many2one(
        comodel_name='account.account',
        string='Default Revenue Account',
        domain="[('company_ids', 'parent_of', company_id), "
               "('account_type', 'not in', "
               "('asset_receivable', 'liability_payable', 'off_balance'))]",
        ondelete='set null',
        help='Fallback revenue account when the product has no income account set.',
    )
    import_run_ids = fields.One2many(
        comodel_name='stripe.import.run',
        inverse_name='stripe_account_id',
        string='Import Runs',
        help='History of Stripe import runs for this account.',
    )

    @staticmethod
    def _normalize_stripe_account_identifier(identifier):
        identifier = (identifier or '').strip()
        return identifier or False

    @staticmethod
    def _legacy_placeholder_stripe_account_identifier(record_id):
        return legacy_placeholder_stripe_account_identifier(record_id)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('stripe_account_identifier'):
                vals['stripe_account_identifier'] = self._normalize_stripe_account_identifier(
                    vals['stripe_account_identifier']
                )
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('stripe_account_identifier'):
            vals = dict(
                vals,
                stripe_account_identifier=self._normalize_stripe_account_identifier(
                    vals['stripe_account_identifier']
                ),
            )
        return super().write(vals)

    def _get_stripe_service(self):
        self.ensure_one()
        return StripeApiService(self.api_key)

    @api.constrains('stripe_account_identifier')
    def _check_stripe_account_identifier(self):
        for account in self:
            account_identifier = self._normalize_stripe_account_identifier(
                account.stripe_account_identifier
            ) or ''
            if account_identifier and not re.fullmatch(r'acct_[A-Za-z0-9]+', account_identifier):
                raise ValidationError(
                    _('Stripe Account ID must start with acct_ and contain only letters and numbers.')
                )

    def _create_import_run(self):
        """Create the import run on the current cursor."""
        self.ensure_one()
        return self.env['stripe.import.run'].create({
            'stripe_account_id': self.id,
            'state': 'running',
            'message': _('Stripe fetch is running.'),
        })

    def _commit_import_progress(self, processed=0, remaining=None):
        """Commit cron work so a worker reload cannot roll back the whole run.

        ``ir.cron._commit_progress`` also tells us when the current cron worker
        is out of time, letting the next scheduled run resume via deduplication.
        Keep direct unit-test calls transactional so TransactionCase can roll
        them back normally.
        """
        if not self.env.context.get('stripe_commit_progress') or config.get('test_enable'):
            return True

        commit_progress = getattr(self.env['ir.cron'].sudo(), '_commit_progress', None)
        if commit_progress:
            seconds_left = commit_progress(processed=processed, remaining=remaining)
            return seconds_left is None or seconds_left > 0

        self.env.cr.commit()
        return True

    def _reap_stale_running_runs(self):
        """Fail any leftover ``running`` runs before starting a new fetch.

        If a previous cron worker crashed mid-fetch the import-run record stays
        ``running`` forever and the dashboard misleads the user into thinking
        a fetch is still in progress. ir.cron serialises ``_cron_fetch_all``,
        so there is no risk of clobbering a concurrent live run.
        """
        self.ensure_one()
        stale = self.env['stripe.import.run'].search([
            ('stripe_account_id', '=', self.id),
            ('state', '=', 'running'),
        ])
        if stale:
            stale.write({
                'state': 'failed',
                'finished_at': fields.Datetime.now(),
                'message': _('Import run abandoned (a newer fetch superseded it).'),
            })

    def _record_import_line(self, run, stripe_object, stripe_object_type, state, message, move=False):
        return self.env['stripe.import.run.line'].create({
            'run_id': run.id,
            'stripe_object_type': stripe_object_type,
            'stripe_object_id': stripe_object.get('id'),
            'state': state,
            'move_id': move.id if move else False,
            'message': message,
        })

    def _prefetch_stripe_data(self, stripe_obj, service, stripe_object_type):
        """Fetch remote Stripe data needed to build the accounting move.

        Stripe API calls (HTTP) inside a gevent savepoint can trigger a
        greenlet switch that closes the DB cursor, corrupting the transaction.
        Pre-fetching customer/line data here keeps the savepoint free of
        network I/O. PDF downloads are deliberately left until after the move
        and import line have been committed, because PDFs are non-accounting
        metadata and can be retried or skipped without losing the import.
        """
        result = {}
        stripe_customer_id = stripe_obj.get('customer')
        if stripe_customer_id:
            existing_partner = self.env['res.partner'].search(
                [('stripe_customer_id', '=', stripe_customer_id)], limit=1
            )
            if not existing_partner:
                result['customer_data'] = service.get_customer(stripe_customer_id)
        result['stripe_lines'] = self._get_stripe_lines(stripe_obj, service, stripe_object_type)
        return result

    def _resolve_partner(self, stripe_customer_id, service=None, customer_data=None):
        partner = self.env['res.partner'].search(
            [('stripe_customer_id', '=', stripe_customer_id)], limit=1
        )
        if partner:
            if not partner.stripe_account_id:
                partner.stripe_account_id = self.id
            return partner
        if customer_data is None and service is not None:
            customer_data = service.get_customer(stripe_customer_id)
        vals = self._build_partner_vals_from_stripe_customer(
            customer_data or {}, stripe_customer_id
        )
        return self._create_partner_with_vat_fallback(vals)

    def _create_partner_with_vat_fallback(self, vals):
        """Create the partner; if Odoo rejects the VAT, drop it and retry once.

        Stripe-supplied VAT values occasionally fail Odoo's ``base_vat`` checks
        (wrong country prefix for the type, free-text noise, format change…).
        We never want a single bad VAT to fail the whole invoice import, so we
        log a warning and create the partner without the VAT. ``is_company``
        is kept ``True`` — the customer was clearly a business in Stripe,
        only the VAT field is unusable.
        """
        Partner = self.env['res.partner']
        if not vals.get('vat'):
            return Partner.create(vals)
        try:
            with self.env.cr.savepoint():
                return Partner.create(vals)
        except ValidationError as error:
            bad_vat = vals.pop('vat', None)
            _logger.warning(
                'Stripe customer %s: VAT %r rejected by Odoo (%s); creating '
                'partner without VAT.',
                vals.get('stripe_customer_id'), bad_vat, error,
            )
            return Partner.create(vals)

    def _build_partner_vals_from_stripe_customer(self, customer_data, stripe_customer_id):
        """Map a Stripe customer payload to ``res.partner`` create-vals.

        Stripe's ``customer.name`` doubles as the business name when the
        customer was collected through a B2B flow; we use it directly for
        ``res.partner.name``. Email always lands in ``email`` regardless of
        whether ``name`` falls back to it.
        """
        business_name = customer_data.get('name')
        email = customer_data.get('email') or ''
        vals = {
            'name': business_name or email or stripe_customer_id,
            'email': email,
            'phone': customer_data.get('phone') or '',
            'stripe_customer_id': stripe_customer_id,
            'stripe_account_id': self.id,
            'customer_rank': 1,
        }

        address = customer_data.get('address') or {}
        if address.get('line1'):
            vals['street'] = address['line1']
        if address.get('line2'):
            vals['street2'] = address['line2']
        if address.get('city'):
            vals['city'] = address['city']
        if address.get('postal_code'):
            vals['zip'] = address['postal_code']

        # ``customer.tax.location.country`` (set when Stripe Tax determines a
        # location) wins over the postal address country; fall back to the
        # address country if tax determination is absent.
        country_code = self._stripe_tax_country_code(customer_data) or address.get('country')
        country = self._resolve_country(country_code)
        if country:
            vals['country_id'] = country.id
            if address.get('state'):
                state = self._resolve_state(address['state'], country)
                if state:
                    vals['state_id'] = state.id

        vat = self._stripe_first_vat_id(customer_data)
        if vat:
            vals['vat'] = vat
            vals['is_company'] = True
        return vals

    def _resolve_country(self, country_code):
        if not country_code:
            return self.env['res.country']
        return self.env['res.country'].search(
            [('code', '=', country_code.upper())], limit=1
        )

    def _resolve_state(self, state_value, country):
        if not state_value or not country:
            return self.env['res.country.state']
        State = self.env['res.country.state']
        state = State.search(
            [('country_id', '=', country.id), ('code', '=', state_value)], limit=1
        )
        if not state:
            state = State.search(
                [('country_id', '=', country.id), ('name', '=ilike', state_value)], limit=1
            )
        return state

    def _stripe_tax_country_code(self, customer_data):
        location = (customer_data.get('tax') or {}).get('location') or {}
        return location.get('country')

    def _stripe_first_vat_id(self, customer_data):
        # Stripe returns ``tax_ids`` as a List object ({object: 'list', data: [...]}).
        # Tolerate a bare list for robustness against API/SDK shape drift.
        raw = customer_data.get('tax_ids')
        if isinstance(raw, dict):
            entries = raw.get('data') or []
        elif isinstance(raw, list):
            entries = raw
        else:
            entries = []
        for entry in entries:
            value = (entry or {}).get('value')
            if value:
                return value
        return False

    def _resolve_product(self, stripe_product_id, product_name):
        product_tmpl = self.env['product.template'].search(
            [('stripe_product_id', '=', stripe_product_id)], limit=1
        )
        if not product_tmpl:
            product_name = self._clean_stripe_product_name(product_name)
            product_tmpl = self.env['product.template'].create({
                'name': product_name or stripe_product_id,
                'stripe_product_id': stripe_product_id,
                'stripe_account_id': self.id,
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
            })
        elif not product_tmpl.stripe_account_id:
            product_tmpl.stripe_account_id = self.id
        return product_tmpl

    def _clean_stripe_product_name(self, product_name):
        clean_name = (product_name or '').strip()
        clean_name = _STRIPE_DESCRIPTION_QUANTITY_RE.sub('', clean_name)
        clean_name = _STRIPE_DESCRIPTION_PRICE_RE.sub('', clean_name)
        return clean_name.strip()

    def _get_line_product_id(self, line):
        pricing = line.get('pricing') or {}
        price_details = pricing.get('price_details') or {}
        product_id = price_details.get('product')
        if not product_id:
            price = line.get('price') or {}
            product_id = price.get('product')
        return product_id

    def _get_line_price_unit(self, line):
        quantity = line.get('quantity') or 1
        pricing = line.get('pricing') or {}
        unit_amount = pricing.get('unit_amount_decimal') or pricing.get('unit_amount')
        if unit_amount is None:
            price = line.get('price') or {}
            unit_amount = price.get('unit_amount_decimal') or price.get('unit_amount')
        if unit_amount is not None:
            return float(unit_amount) / 100.0
        return ((line.get('amount') or 0) / 100.0) / quantity

    def _has_deferred_date_fields(self):
        move_line_fields = self.env['account.move.line']._fields
        return (
            'deferred_start_date' in move_line_fields
            and 'deferred_end_date' in move_line_fields
        )

    def _stripe_timestamp_to_date(self, timestamp):
        if timestamp is None:
            return False
        try:
            return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date()
        except (OSError, TypeError, ValueError, OverflowError):
            return False

    def _get_line_deferred_date_vals(self, line):
        if not self._has_deferred_date_fields():
            return {}

        period = line.get('period') or {}
        start_date = self._stripe_timestamp_to_date(period.get('start'))
        end_date = self._stripe_timestamp_to_date(period.get('end'))
        if not start_date or not end_date or start_date > end_date:
            return {}

        return {
            'deferred_start_date': start_date,
            'deferred_end_date': end_date,
        }

    def _get_line_account_id(self, product_tmpl):
        if product_tmpl and product_tmpl.property_account_income_id:
            return product_tmpl.property_account_income_id.id
        if self.default_revenue_account_id:
            return self.default_revenue_account_id.id
        return False

    def _prepare_move_line_vals(self, line):
        stripe_product_id = self._get_line_product_id(line)
        product_name = line.get('description') or ''
        product_tmpl = False
        if stripe_product_id:
            product_tmpl = self._resolve_product(stripe_product_id, product_name)

        line_vals = {
            'name': line.get('description') or (product_tmpl.name if product_tmpl else '/'),
            'quantity': line.get('quantity') or 1,
            'price_unit': self._get_line_price_unit(line),
        }
        line_vals.update(self._get_line_deferred_date_vals(line))
        if product_tmpl and product_tmpl.product_variant_ids:
            line_vals['product_id'] = product_tmpl.product_variant_ids[:1].id

        account_id = self._get_line_account_id(product_tmpl)
        if account_id:
            line_vals['account_id'] = account_id
        return line_vals

    def _build_move_lines(self, stripe_lines):
        return [(0, 0, self._prepare_move_line_vals(line)) for line in stripe_lines]

    def _attach_pdf(self, move, stripe_obj, service=None, pdf_field='invoice_pdf', pdf_bytes=None):
        if pdf_bytes is None:
            pdf_url = stripe_obj.get(pdf_field)
            if not pdf_url or service is None:
                return
            try:
                pdf_bytes = service.get_pdf(pdf_url)
            except Exception as exc:
                _logger.warning('Could not fetch Stripe PDF for %s: %s', stripe_obj.get('id'), exc)
                return
        try:
            self.env['ir.attachment'].create({
                'name': 'stripe_%s_%s.pdf' % (pdf_field.replace('_pdf', ''), stripe_obj['id']),
                'datas': base64.b64encode(pdf_bytes),
                'mimetype': 'application/pdf',
                'res_model': 'account.move',
                'res_id': move.id,
            })
        except Exception as error:
            if _is_closed_db_connection_error(error):
                raise
            _logger.exception('Could not attach Stripe PDF for %s', stripe_obj.get('id'))

    def _get_existing_move(self, stripe_id):
        return self.env['account.move'].search(
            [('stripe_invoice_id', '=', stripe_id)], limit=1
        )

    def _get_existing_moves_by_stripe_id(self, stripe_ids):
        stripe_ids = [stripe_id for stripe_id in dict.fromkeys(stripe_ids) if stripe_id]
        if not stripe_ids:
            return {}
        moves = self.env['account.move'].search([('stripe_invoice_id', 'in', stripe_ids)])
        return {
            move.stripe_invoice_id: move
            for move in moves
            if move.stripe_invoice_id
        }

    def _resolve_currency(self, currency_code):
        if not currency_code:
            return self.env['res.currency']
        return self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', currency_code.upper())], limit=1
        )

    def _get_invoice_date(self, stripe_obj):
        # Prefer the finalization timestamp so accounting-period assignment matches
        # the date the invoice was issued, not the date a draft was first created.
        status_transitions = stripe_obj.get('status_transitions') or {}
        finalized_ts = status_transitions.get('finalized_at')
        ts = finalized_ts or stripe_obj.get('created')
        if not ts:
            return False
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()

    def _get_invoice_date_due(self, stripe_obj):
        due_ts = stripe_obj.get('due_date')
        if not due_ts:
            return False
        return datetime.fromtimestamp(due_ts, tz=timezone.utc).date()

    def _get_credit_note_line_invoice_line_id(self, credit_note_line):
        return credit_note_line.get('invoice_line_item') or credit_note_line.get('invoice_line')

    def _add_invoice_line_periods_to_credit_note_lines(self, stripe_obj, credit_note_lines, service):
        invoice_id = stripe_obj.get('invoice')
        if not invoice_id:
            return credit_note_lines

        invoice_line_ids = set()
        for line in credit_note_lines:
            if line.get('period'):
                continue
            invoice_line_id = self._get_credit_note_line_invoice_line_id(line)
            if invoice_line_id:
                invoice_line_ids.add(invoice_line_id)
        if not invoice_line_ids:
            return credit_note_lines

        try:
            invoice_lines = service.get_invoice_lines(invoice_id)
        except Exception as exc:
            _logger.warning(
                'Could not fetch Stripe invoice lines for credit note %s deferred periods: %s',
                stripe_obj.get('id'), exc,
            )
            return credit_note_lines

        invoice_lines_by_id = {
            invoice_line.get('id'): invoice_line
            for invoice_line in invoice_lines
            if invoice_line.get('id') in invoice_line_ids
        }
        enriched_lines = []
        for line in credit_note_lines:
            period = line.get('period')
            invoice_line_id = self._get_credit_note_line_invoice_line_id(line)
            if not period and invoice_line_id:
                period = (invoice_lines_by_id.get(invoice_line_id) or {}).get('period')
                if period:
                    line = dict(line, period=period)
            enriched_lines.append(line)
        return enriched_lines

    def _get_stripe_lines(self, stripe_obj, service, stripe_object_type):
        stripe_id = stripe_obj['id']
        if stripe_object_type == 'credit_note':
            credit_note_lines = service.get_credit_note_lines(stripe_id)
            return self._add_invoice_line_periods_to_credit_note_lines(
                stripe_obj, credit_note_lines, service
            )
        return service.get_invoice_lines(stripe_id)

    def _prepare_move_vals(self, stripe_obj, service, move_type, stripe_object_type, prefetched=None):
        stripe_customer_id = stripe_obj.get('customer')
        if not stripe_customer_id:
            raise ValueError('Stripe object has no customer.')

        customer_data = prefetched.get('customer_data') if prefetched else None
        partner = self._resolve_partner(stripe_customer_id, service=service, customer_data=customer_data)
        stripe_lines = prefetched['stripe_lines'] if prefetched else self._get_stripe_lines(stripe_obj, service, stripe_object_type)
        vals = {
            'move_type': move_type,
            'journal_id': self.sales_journal_id.id,
            'partner_id': partner.id,
            'invoice_date': self._get_invoice_date(stripe_obj),
            'invoice_date_due': self._get_invoice_date_due(stripe_obj),
            'stripe_invoice_id': stripe_obj['id'],
            'stripe_account_id': self.id,
            'stripe_object_type': stripe_object_type,
            'invoice_line_ids': self._build_move_lines(stripe_lines),
        }
        currency = self._resolve_currency(stripe_obj.get('currency'))
        if currency:
            vals['currency_id'] = currency.id
        return vals

    def _post_if_configured(self, move):
        if self.auto_confirm:
            # ``disable_abnormal_invoice_detection`` skips Odoo's anomaly heuristic,
            # which produces frequent false positives during bulk historical imports.
            move.with_context(disable_abnormal_invoice_detection=True).action_post()

    def _process_stripe_object(
        self,
        stripe_obj,
        service,
        move_type,
        stripe_object_type,
        prefetched=None,
        skip_existing_lookup=False,
    ):
        stripe_id = stripe_obj.get('id')
        if not stripe_id:
            raise ValueError('Stripe object has no ID.')

        if not skip_existing_lookup:
            existing = self._get_existing_move(stripe_id)
            if existing:
                if not existing.stripe_account_id:
                    existing.stripe_account_id = self.id
                return existing, 'skipped', _('Already imported.')

        move_vals = self._prepare_move_vals(stripe_obj, service, move_type, stripe_object_type, prefetched=prefetched)
        move = self.env['account.move'].create(move_vals)
        self._post_if_configured(move)
        return move, 'done', _('Imported as %s.', move.display_name)

    def _process_stripe_invoice(self, stripe_invoice, service, move_type='out_invoice'):
        stripe_object_type = 'credit_note' if move_type == 'out_refund' else 'invoice'
        move, _state, _message = self._process_stripe_object(
            stripe_invoice, service, move_type, stripe_object_type
        )
        return move

    def _get_invoice_fetch_floor(self):
        """Lower bound for `created` when listing invoices.

        Late-finalized invoices (e.g. subscription drafts created weeks before
        finalization) would be missed if we used ``last_fetch_at`` directly, so
        we always re-scan a rolling lookback window. Already-imported invoices
        are skipped by the SQL UNIQUE on ``account.move.stripe_invoice_id``.
        """
        self.ensure_one()
        floor = False
        if self.last_fetch_at:
            lookback = self.fetch_lookback_days or 0
            floor = self.last_fetch_at - timedelta(days=lookback) if lookback else self.last_fetch_at
        cutoff = self.invoice_cutoff_date
        if cutoff:
            cutoff_dt = datetime.combine(cutoff, datetime.min.time())
            if not floor or cutoff_dt > floor:
                floor = cutoff_dt
        return floor

    def _get_credit_note_fetch_floor(self):
        """Lower bound for ``created`` when listing credit notes."""
        self.ensure_one()
        floor = self.last_fetch_at
        cutoff = self.invoice_cutoff_date
        if cutoff:
            cutoff_dt = datetime.combine(cutoff, datetime.min.time())
            # Stripe's API only supports ``created[gt]``. Step back one second
            # so credit notes created exactly at the start of the cut-off date
            # remain eligible, then let the service-side filter enforce >=.
            cutoff_query_floor = cutoff_dt - timedelta(seconds=1)
            if not floor or cutoff_query_floor > floor:
                floor = cutoff_query_floor
        return floor

    def _fetch_stripe_objects(self, service):
        return [
            (
                'invoice',
                'out_invoice',
                service.get_invoices(
                    created_after=self._get_invoice_fetch_floor(),
                    finalized_after=self.invoice_cutoff_date,
                ),
            ),
            (
                'credit_note',
                'out_refund',
                service.get_credit_notes(
                    created_after=self._get_credit_note_fetch_floor(),
                    created_on_or_after=self.invoice_cutoff_date,
                ),
            ),
        ]

    def _process_batch_item(
        self,
        run,
        stripe_obj,
        service,
        move_type,
        stripe_object_type,
        remaining_after=None,
        existing_moves_by_stripe_id=None,
    ):
        stripe_id = stripe_obj.get('id')
        existing_move = (
            existing_moves_by_stripe_id.get(stripe_id)
            if existing_moves_by_stripe_id is not None and stripe_id
            else False
        )
        if existing_move:
            if not existing_move.stripe_account_id:
                existing_move.stripe_account_id = self.id
            self._record_import_line(
                run,
                stripe_obj,
                stripe_object_type,
                'skipped',
                _('Already imported.'),
                move=existing_move,
            )
            keep_going = self._commit_import_progress(processed=1, remaining=remaining_after)
            return 'skipped', False, keep_going

        # Pre-fetch all Stripe API data before entering the DB savepoint.
        # HTTP calls inside a savepoint can trigger gevent greenlet switches
        # that close the cursor, breaking both the rollback and any follow-up
        # DB writes (e.g. recording the failure itself).
        try:
            prefetched = self._prefetch_stripe_data(stripe_obj, service, stripe_object_type)
        except Exception as error:
            if _is_closed_db_connection_error(error):
                raise
            message = '%s: %s' % (error.__class__.__name__, error)
            _logger.exception('Error prefetching Stripe data for %s %s', stripe_object_type, stripe_id)
            self._record_import_line(run, stripe_obj, stripe_object_type, 'failed', message)
            keep_going = self._commit_import_progress(processed=1, remaining=remaining_after)
            return 'failed', message, keep_going

        try:
            with self.env.cr.savepoint():
                move, state, message = self._process_stripe_object(
                    stripe_obj,
                    service,
                    move_type,
                    stripe_object_type,
                    prefetched=prefetched,
                    skip_existing_lookup=existing_moves_by_stripe_id is not None,
                )
            self._record_import_line(run, stripe_obj, stripe_object_type, state, message, move=move)
            if existing_moves_by_stripe_id is not None and stripe_id and move:
                existing_moves_by_stripe_id[stripe_id] = move
            keep_going = self._commit_import_progress(
                processed=1,
                remaining=remaining_after,
            )
            # Optional PDFs are fetched only after the accounting result is durable.
            if state == 'done' and keep_going:
                pdf_field = 'pdf' if stripe_object_type == 'credit_note' else 'invoice_pdf'
                self._attach_pdf(
                    move, stripe_obj,
                    service=service,
                    pdf_field=pdf_field,
                )
                keep_going = self._commit_import_progress(remaining=remaining_after)
            return state, False, keep_going
        except Exception as error:
            if _is_closed_db_connection_error(error):
                raise
            if _is_duplicate_stripe_move_error(error) and stripe_id:
                existing_move = self._get_existing_move(stripe_id)
                if existing_move:
                    if not existing_move.stripe_account_id:
                        existing_move.stripe_account_id = self.id
                    if existing_moves_by_stripe_id is not None:
                        existing_moves_by_stripe_id[stripe_id] = existing_move
                    self._record_import_line(
                        run,
                        stripe_obj,
                        stripe_object_type,
                        'skipped',
                        _('Already imported.'),
                        move=existing_move,
                    )
                    keep_going = self._commit_import_progress(
                        processed=1,
                        remaining=remaining_after,
                    )
                    return 'skipped', False, keep_going
            message = '%s: %s' % (error.__class__.__name__, error)
            _logger.exception('Error processing Stripe %s %s', stripe_object_type, stripe_id)
            try:
                self._record_import_line(run, stripe_obj, stripe_object_type, 'failed', message)
                keep_going = self._commit_import_progress(
                    processed=1,
                    remaining=remaining_after,
                )
            except Exception as line_error:
                if _is_closed_db_connection_error(line_error):
                    raise
                _logger.exception(
                    'Could not record failure line for Stripe %s %s', stripe_object_type, stripe_id
                )
                keep_going = True
            return 'failed', message, keep_going

    def _finish_import_run(self, run, counts, failures):
        finished_at = fields.Datetime.now()
        state = 'done'
        if failures and counts['done']:
            state = 'partial'
        elif failures:
            state = 'failed'

        summary = _(
            'Imported %(done)s Stripe objects, skipped %(skipped)s, failed %(failed)s.'
        ) % counts
        if failures:
            summary = '%s\n\n%s\n%s' % (
                summary,
                _('Failures:'),
                '\n'.join('- %s' % failure for failure in failures),
            )

        run.write({
            'state': state,
            'finished_at': finished_at,
            'invoice_count': counts['invoice'],
            'credit_note_count': counts['credit_note'],
            'skipped_count': counts['skipped'],
            'error_count': counts['failed'],
            'message': summary,
        })
        run.message_post(body='<br/>'.join(html_escape(line) for line in summary.splitlines()))
        return state

    @api.model
    def _fetch_queue_domain(self):
        return [
            ('active', '=', True),
            ('fetch_requested_at', '!=', False),
            ('fetch_started_at', '=', False),
        ]

    @api.model
    def _fetch_queue_stale_cutoff(self):
        limit_time = config['limit_time_real_cron'] or -1
        if limit_time <= 0:
            limit_time = config['limit_time_real'] or 120
        return fields.Datetime.now() - timedelta(seconds=limit_time + 20)

    @api.model
    def _trigger_fetch_worker(self):
        cron = self.env.ref(
            'stripe_connector.cron_stripe_process_fetch_queue',
            raise_if_not_found=False,
        )
        if not cron:
            _logger.error(
                'Stripe fetch queue cron not found '
                '(stripe_connector.cron_stripe_process_fetch_queue)'
            )
            return
        _logger.info('Stripe fetch queue: triggering cron id=%d active=%s', cron.id, cron.active)
        cron.sudo()._trigger(fields.Datetime.now() + timedelta(seconds=1))

    def _queue_fetch(self, source='manual'):
        if source not in ('manual', 'scheduled'):
            source = 'manual'
        accounts = self.sudo().filtered('active')
        requested_at = fields.Datetime.now()
        for account in accounts:
            account_requested_at = requested_at
            if account.fetch_requested_at and account_requested_at <= account.fetch_requested_at:
                account_requested_at = account.fetch_requested_at + timedelta(seconds=1)
            account.write({
                'fetch_requested_at': account_requested_at,
                'fetch_request_source': source,
            })
        return accounts

    def _clear_fetch_queue(self, claimed_requested_at=False):
        self.ensure_one()
        vals = {'fetch_started_at': False}
        if (
            not self.fetch_requested_at
            or not claimed_requested_at
            or self.fetch_requested_at == claimed_requested_at
        ):
            vals.update({
                'fetch_requested_at': False,
                'fetch_request_source': False,
            })
        self.write(vals)

    @api.model
    def _requeue_stale_fetches(self):
        cutoff = self._fetch_queue_stale_cutoff()
        stale_accounts = self.sudo().search([
            ('active', '=', True),
            ('fetch_requested_at', '!=', False),
            ('fetch_started_at', '!=', False),
            ('fetch_started_at', '<=', cutoff),
        ])
        if stale_accounts:
            _logger.warning(
                'Stripe fetch queue: requeueing %d stale account(s): %s',
                len(stale_accounts),
                ', '.join(stale_accounts.mapped('display_name')),
            )
            stale_accounts.write({'fetch_started_at': False})
        return stale_accounts

    def _trigger_fetch(self):
        """Queue this account and trigger the Stripe fetch worker cron."""
        self.ensure_one()
        self._queue_fetch(source='manual')
        self._trigger_fetch_worker()

    @api.model
    def _cron_queue_all_fetches(self):
        """Queue a fetch for every active Stripe account. Called by the daily cron.

        Uses ``sudo()`` so the import-side creates (partner, product, move) do
        not depend on the cron user being a Stripe admin - the access checks
        on those models bail on ``env.su`` before consulting groups.
        """
        accounts = self.sudo().search([('active', '=', True)])
        queued = accounts._queue_fetch(source='scheduled')
        self.env['ir.cron']._commit_progress(processed=len(accounts), remaining=0)
        _logger.info(
            'Stripe cron: queued %d of %d active account(s)',
            len(queued),
            len(accounts),
        )
        if accounts:
            self._trigger_fetch_worker()

    @api.model
    def _cron_process_fetch_queue(self):
        """Process one queued Stripe account and reschedule if more are pending."""
        Account = self.sudo()
        Account._requeue_stale_fetches()
        account = Account.search(Account._fetch_queue_domain(), order='fetch_requested_at, id', limit=1)
        if not account:
            self.env['ir.cron']._commit_progress(remaining=0)
            return

        claimed_requested_at = account.fetch_requested_at
        started_at = fields.Datetime.now()
        account.write({'fetch_started_at': started_at})
        self.env['ir.cron']._commit_progress(remaining=1)
        try:
            account.with_context(stripe_commit_progress=True)._fetch_invoices()
        except Exception:
            _logger.exception(
                'Unhandled error during queued Stripe invoice fetch for account %s',
                account.display_name,
            )
        finally:
            account.invalidate_recordset(['fetch_requested_at', 'fetch_started_at'])
            account._clear_fetch_queue(claimed_requested_at=claimed_requested_at)

        remaining = Account.search_count(Account._fetch_queue_domain())
        self.env['ir.cron']._commit_progress(processed=1, remaining=0)
        if remaining:
            self._trigger_fetch_worker()

    @api.model
    def _cron_fetch_all(self):
        """Compatibility wrapper for databases still pointing at the old cron code.

        Uses ``sudo()`` so the import-side creates (partner, product, move) do
        not depend on the cron user being a Stripe admin — the access checks
        on those models bail on ``env.su`` before consulting groups.
        """
        return self._cron_queue_all_fetches()

    def _fetch_invoices(self):
        self.ensure_one()
        fetch_started_at = fields.Datetime.now()
        self._reap_stale_running_runs()
        run = self._create_import_run()
        run.message_post(body=_('Stripe fetch started for %s.', self.display_name))
        _logger.info('Stripe fetch started for account %s', self.display_name)
        self._commit_import_progress()

        counts = {
            'invoice': 0,
            'credit_note': 0,
            'done': 0,
            'skipped': 0,
            'failed': 0,
        }
        failures = []

        try:
            service = self._get_stripe_service()
            batches = self._fetch_stripe_objects(service)
        except Exception as error:
            message = '%s: %s' % (error.__class__.__name__, error)
            _logger.exception('Could not fetch Stripe object lists for account %s', self.display_name)
            failures.append(message)
            batches = []

        remaining = sum(len(stripe_objects) for _, _, stripe_objects in batches)
        stopped_early = False
        if remaining and not self._commit_import_progress(remaining=remaining):
            failures.append(_CRON_TIME_BUDGET_MESSAGE)
            batches = []
            remaining = 0
            stopped_early = True
        existing_moves_by_stripe_id = (
            self._get_existing_moves_by_stripe_id(
                stripe_obj.get('id')
                for _stripe_object_type, _move_type, stripe_objects in batches
                for stripe_obj in stripe_objects
            )
            if batches
            else {}
        )
        stop_requested = False
        for stripe_object_type, move_type, stripe_objects in batches:
            counts[stripe_object_type] += len(stripe_objects)
            for stripe_obj in stripe_objects:
                remaining_after = remaining - 1
                state, failure, keep_going = self._process_batch_item(
                    run,
                    stripe_obj,
                    service,
                    move_type,
                    stripe_object_type,
                    remaining_after=remaining_after,
                    existing_moves_by_stripe_id=existing_moves_by_stripe_id,
                )
                remaining = remaining_after
                counts[state] += 1
                if failure:
                    failures.append('%s %s: %s' % (
                        stripe_object_type,
                        stripe_obj.get('id'),
                        failure,
                    ))
                if not keep_going:
                    failures.append(_CRON_TIME_BUDGET_MESSAGE)
                    stop_requested = True
                    stopped_early = True
                    break
            if stop_requested:
                break

        state = self._finish_import_run(run, counts, failures)
        if state == 'done':
            self.last_fetch_at = fetch_started_at
        else:
            _logger.warning(
                'Stripe fetch for account %s finished with state %s; watermark not advanced.',
                self.display_name,
                state,
            )
        if stopped_early:
            # Re-queue this account so the worker picks it up again for the
            # remaining Stripe objects.  A distinct fetch_requested_at value
            # tells _clear_fetch_queue to leave the request in place.
            source = self.fetch_request_source or 'scheduled'
            self._queue_fetch(source=source)
        self._commit_import_progress(remaining=0)
        return run

import base64
import logging
import re
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import html_escape

from ..services.stripe_api import StripeApiService

_logger = logging.getLogger(__name__)
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


class StripeAccount(models.Model):
    _name = 'stripe.account'
    _description = 'Stripe Account Configuration'
    _order = 'name'
    _check_company_auto = True

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
            'Invoices finalized before this date are ignored. Use this to avoid '
            'importing historical invoices that were already handled manually.'
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

    def _get_stripe_service(self):
        self.ensure_one()
        return StripeApiService(self.api_key)

    def _create_import_run(self):
        """Create the import run on the current cursor.

        We deliberately do NOT use ``self.env.registry.cursor()`` to commit the
        run early for mid-fetch UI visibility: Odoo runs cursors at REPEATABLE
        READ isolation, so the main cursor's snapshot is taken at its first
        ``SELECT`` and excludes any commits made afterwards by another cursor.
        Subsequent reads on the run from the main cursor (notably the ones
        ``mail.thread.message_post`` performs) would then raise ``MissingError``
        even though the row is in the database.

        The trade-off is that other connections (e.g. a browser viewing the
        Stripe Account form) only see the run after the cron transaction
        commits at end-of-job; for a typical daily fetch that is a matter of
        seconds, which is acceptable.
        """
        self.ensure_one()
        return self.env['stripe.import.run'].create({
            'stripe_account_id': self.id,
            'state': 'running',
            'message': _('Stripe fetch is running.'),
        })

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
        """Fetch all remote Stripe data before entering any DB savepoint.

        Stripe API calls (HTTP) inside a gevent savepoint can trigger a
        greenlet switch that closes the DB cursor, corrupting the transaction.
        Pre-fetching here keeps the savepoint free of network I/O.
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
        pdf_field = 'pdf' if stripe_object_type == 'credit_note' else 'invoice_pdf'
        pdf_url = stripe_obj.get(pdf_field)
        if pdf_url:
            try:
                result['pdf_bytes'] = service.get_pdf(pdf_url)
                result['pdf_field'] = pdf_field
            except Exception as exc:
                _logger.warning(
                    'Could not prefetch Stripe PDF for %s: %s', stripe_obj.get('id'), exc
                )
        return result

    def _resolve_partner(self, stripe_customer_id, service=None, customer_data=None):
        partner = self.env['res.partner'].search(
            [('stripe_customer_id', '=', stripe_customer_id)], limit=1
        )
        if partner:
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
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
            })
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
        except Exception:
            _logger.exception('Could not attach Stripe PDF for %s', stripe_obj.get('id'))

    def _get_existing_move(self, stripe_id):
        return self.env['account.move'].search(
            [('stripe_invoice_id', '=', stripe_id)], limit=1
        )

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

    def _get_stripe_lines(self, stripe_obj, service, stripe_object_type):
        stripe_id = stripe_obj['id']
        if stripe_object_type == 'credit_note':
            return service.get_credit_note_lines(stripe_id)
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

    def _process_stripe_object(self, stripe_obj, service, move_type, stripe_object_type, prefetched=None):
        stripe_id = stripe_obj.get('id')
        if not stripe_id:
            raise ValueError('Stripe object has no ID.')

        existing = self._get_existing_move(stripe_id)
        if existing:
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
            ('credit_note', 'out_refund', service.get_credit_notes(created_after=self.last_fetch_at)),
        ]

    def _process_batch_item(self, run, stripe_obj, service, move_type, stripe_object_type):
        stripe_id = stripe_obj.get('id')
        # Pre-fetch all Stripe API data before entering the DB savepoint.
        # HTTP calls inside a savepoint can trigger gevent greenlet switches
        # that close the cursor, breaking both the rollback and any follow-up
        # DB writes (e.g. recording the failure itself).
        try:
            prefetched = self._prefetch_stripe_data(stripe_obj, service, stripe_object_type)
        except Exception as error:
            message = '%s: %s' % (error.__class__.__name__, error)
            _logger.exception('Error prefetching Stripe data for %s %s', stripe_object_type, stripe_id)
            self._record_import_line(run, stripe_obj, stripe_object_type, 'failed', message)
            return 'failed', message

        try:
            with self.env.cr.savepoint():
                move, state, message = self._process_stripe_object(
                    stripe_obj, service, move_type, stripe_object_type, prefetched=prefetched
                )
            # PDF attachment happens outside the savepoint so that the HTTP call to
            # download the PDF cannot trigger a gevent greenlet switch while a DB
            # savepoint is active, which would corrupt the cursor for all subsequent
            # operations in this request.
            if state == 'done':
                pdf_field = 'pdf' if stripe_object_type == 'credit_note' else 'invoice_pdf'
                pdf_bytes = prefetched.get('pdf_bytes') if prefetched else None
                # If prefetch already attempted the download (pdf_url exists but
                # pdf_bytes is absent), the error was already logged — don't retry
                # with the same URL.
                pdf_url = stripe_obj.get(pdf_field)
                already_attempted = prefetched is not None and pdf_url and 'pdf_bytes' not in prefetched
                self._attach_pdf(
                    move, stripe_obj,
                    service=None if already_attempted else service,
                    pdf_field=pdf_field,
                    pdf_bytes=pdf_bytes,
                )
            self._record_import_line(run, stripe_obj, stripe_object_type, state, message, move=move)
            return state, False
        except Exception as error:
            message = '%s: %s' % (error.__class__.__name__, error)
            _logger.exception('Error processing Stripe %s %s', stripe_object_type, stripe_id)
            try:
                self._record_import_line(run, stripe_obj, stripe_object_type, 'failed', message)
            except Exception:
                _logger.exception(
                    'Could not record failure line for Stripe %s %s', stripe_object_type, stripe_id
                )
            return 'failed', message

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

    def _trigger_fetch(self):
        """Trigger the shared Stripe fetch cron to run in the background.

        Uses a pre-defined single cron record instead of creating a new one per
        click, following the same pattern as account_online_synchronization.
        The trigger fires within the cron worker's next poll cycle (~60 s).
        """
        self.ensure_one()
        cron = self.env.ref('stripe_connector.cron_stripe_fetch_invoices', raise_if_not_found=False)
        if not cron:
            _logger.error('Stripe fetch cron not found (stripe_connector.cron_stripe_fetch_invoices)')
            return
        _logger.info('Stripe fetch: triggering cron id=%d active=%s', cron.id, cron.active)
        cron.sudo()._trigger(fields.Datetime.now() + timedelta(seconds=1))

    @api.model
    def _cron_fetch_all(self):
        """Fetch invoices for every active Stripe account. Called by the cron.

        Uses ``sudo()`` so the import-side creates (partner, product, move) do
        not depend on the cron user being a Stripe admin — the access checks
        on those models bail on ``env.su`` before consulting groups.
        """
        accounts = self.sudo().search([('active', '=', True)])
        _logger.info('Stripe cron: fetching for %d active account(s)', len(accounts))
        for account in accounts:
            try:
                account._fetch_invoices()
            except Exception:
                _logger.exception(
                    'Unhandled error during Stripe invoice fetch for account %s', account.name
                )

    def _fetch_invoices(self):
        self.ensure_one()
        fetch_started_at = fields.Datetime.now()
        self._reap_stale_running_runs()
        run = self._create_import_run()
        run.message_post(body=_('Stripe fetch started for %s.', self.display_name))
        _logger.info('Stripe fetch started for account %s', self.display_name)

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

        for stripe_object_type, move_type, stripe_objects in batches:
            counts[stripe_object_type] += len(stripe_objects)
            for stripe_obj in stripe_objects:
                state, failure = self._process_batch_item(
                    run, stripe_obj, service, move_type, stripe_object_type
                )
                counts[state] += 1
                if failure:
                    failures.append('%s %s: %s' % (
                        stripe_object_type,
                        stripe_obj.get('id'),
                        failure,
                    ))

        state = self._finish_import_run(run, counts, failures)
        if state == 'done':
            self.last_fetch_at = fetch_started_at
        else:
            _logger.warning(
                'Stripe fetch for account %s finished with state %s; watermark not advanced.',
                self.display_name,
                state,
            )
        return run

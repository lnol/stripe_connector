from datetime import date, datetime, time, timezone

import requests

from ..const import STRIPE_API_BASE_URL, STRIPE_REQUEST_TIMEOUT


class StripeApiService:

    def __init__(self, api_key):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.auth = (api_key, '')

    def _get(self, endpoint, params=None):
        url = STRIPE_API_BASE_URL + endpoint
        response = self.session.get(
            url,
            params=params or {},
            timeout=STRIPE_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def _paginate(self, endpoint, base_params):
        items = []
        params = dict(base_params)
        params['limit'] = 100
        while True:
            data = self._get(endpoint, params)
            page_items = data.get('data', [])
            items.extend(page_items)
            if not data.get('has_more'):
                break
            if not page_items:
                break
            params['starting_after'] = page_items[-1]['id']
        return items

    def _to_stripe_timestamp(self, created_after):
        if not created_after:
            return False
        if isinstance(created_after, datetime):
            value = created_after
        elif isinstance(created_after, date):
            value = datetime.combine(created_after, time.min)
        else:
            value = datetime.fromisoformat(str(created_after))
        if not value.tzinfo:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())

    def _created_params(self, created_after):
        timestamp = self._to_stripe_timestamp(created_after)
        return {'created[gt]': timestamp} if timestamp else {}

    def _is_finalized_on_or_after(self, invoice, finalized_after):
        timestamp = self._to_stripe_timestamp(finalized_after)
        if not timestamp:
            return True

        status_transitions = invoice.get('status_transitions') or {}
        finalized_at = status_transitions.get('finalized_at')
        return bool(finalized_at and finalized_at >= timestamp)

    def _is_created_on_or_after(self, stripe_object, created_on_or_after):
        timestamp = self._to_stripe_timestamp(created_on_or_after)
        if not timestamp:
            return True

        created = stripe_object.get('created')
        return bool(created and created >= timestamp)

    def get_invoices(self, created_after=None, finalized_after=None):
        all_invoices = self._paginate('invoices', self._created_params(created_after))
        return [
            inv for inv in all_invoices
            if inv.get('status') != 'draft'
            and self._is_finalized_on_or_after(inv, finalized_after)
        ]

    def get_credit_notes(self, created_after=None, created_on_or_after=None):
        credit_notes = self._paginate('credit_notes', self._created_params(created_after))
        return [
            credit_note for credit_note in credit_notes
            if credit_note.get('status') == 'issued'
            and self._is_created_on_or_after(credit_note, created_on_or_after)
        ]

    def get_customer(self, customer_id):
        # ``tax_ids`` is a sub-resource and is not returned in the default
        # customer payload; expand it so we can populate the partner's VAT.
        return self._get('customers/%s' % customer_id, {'expand[]': ['tax_ids']})

    def get_invoice_lines(self, invoice_id):
        return self._paginate('invoices/%s/lines' % invoice_id, {})

    def get_credit_note_lines(self, credit_note_id):
        return self._paginate('credit_notes/%s/lines' % credit_note_id, {})

    def get_pdf(self, pdf_url):
        response = self.session.get(pdf_url, timeout=STRIPE_REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.content

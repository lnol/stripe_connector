import requests

from ..const import STRIPE_API_BASE_URL


class StripeApiService:

    def __init__(self, api_key):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.auth = (api_key, '')

    def _get(self, endpoint, params=None):
        url = STRIPE_API_BASE_URL + endpoint
        response = self.session.get(url, params=params or {})
        response.raise_for_status()
        return response.json()

    def _paginate(self, endpoint, base_params):
        items = []
        params = dict(base_params)
        params['limit'] = 100
        while True:
            data = self._get(endpoint, params)
            items.extend(data.get('data', []))
            if not data.get('has_more'):
                break
            params['starting_after'] = data['data'][-1]['id']
        return items

    def get_invoices(self, created_after=None):
        params = {}
        if created_after:
            from datetime import datetime, timezone
            ts = int(datetime.combine(
                created_after,
                datetime.min.time(),
                tzinfo=timezone.utc,
            ).timestamp())
            params['created[gt]'] = ts
        all_invoices = self._paginate('invoices', params)
        return [inv for inv in all_invoices if inv.get('status') != 'draft']

    def get_credit_notes(self, created_after=None):
        params = {'status': 'issued'}
        if created_after:
            from datetime import datetime, timezone
            ts = int(datetime.combine(
                created_after,
                datetime.min.time(),
                tzinfo=timezone.utc,
            ).timestamp())
            params['created[gt]'] = ts
        return self._paginate('credit_notes', params)

    def get_customer(self, customer_id):
        return self._get('customers/%s' % customer_id)

    def get_invoice_lines(self, invoice_id):
        return self._paginate('invoices/%s/lines' % invoice_id, {})

    def get_pdf(self, pdf_url):
        response = self.session.get(pdf_url)
        response.raise_for_status()
        return response.content

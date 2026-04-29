from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.stripe_connector.const import STRIPE_REQUEST_TIMEOUT
from odoo.addons.stripe_connector.services.stripe_api import StripeApiService


@tagged('post_install', '-at_install', 'stripe_connector')
class TestStripeApiService(TransactionCase):

    def test_get_uses_timeout(self):
        service = StripeApiService('sk_test_dummy')
        response = MagicMock()
        response.json.return_value = {}

        with patch.object(service.session, 'get', return_value=response) as mock_get:
            service._get('invoices')

        response.raise_for_status.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs['timeout'], STRIPE_REQUEST_TIMEOUT)

    def test_get_pdf_uses_timeout(self):
        service = StripeApiService('sk_test_dummy')
        response = MagicMock()
        response.content = b'%PDF'

        with patch.object(service.session, 'get', return_value=response) as mock_get:
            service.get_pdf('https://stripe.example.com/invoice.pdf')

        response.raise_for_status.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs['timeout'], STRIPE_REQUEST_TIMEOUT)

    def test_credit_note_lines_use_credit_note_endpoint(self):
        service = StripeApiService('sk_test_dummy')

        with patch.object(service, '_paginate', return_value=[]) as mock_paginate:
            service.get_credit_note_lines('cn_TEST')

        mock_paginate.assert_called_once_with('credit_notes/cn_TEST/lines', {})

    def test_get_invoices_filters_by_finalized_after(self):
        service = StripeApiService('sk_test_dummy')
        cutoff_ts = int(datetime(2024, 1, 15, tzinfo=timezone.utc).timestamp())
        invoices = [
            {
                'id': 'in_BEFORE',
                'status': 'paid',
                'status_transitions': {'finalized_at': cutoff_ts - 1},
            },
            {
                'id': 'in_ON_CUTOFF',
                'status': 'open',
                'status_transitions': {'finalized_at': cutoff_ts},
            },
            {
                'id': 'in_AFTER',
                'status': 'paid',
                'status_transitions': {'finalized_at': cutoff_ts + 1},
            },
            {
                'id': 'in_DRAFT',
                'status': 'draft',
                'status_transitions': {'finalized_at': cutoff_ts + 1},
            },
        ]

        with patch.object(service, '_paginate', return_value=invoices):
            result = service.get_invoices(finalized_after=date(2024, 1, 15))

        self.assertEqual(
            [invoice['id'] for invoice in result],
            ['in_ON_CUTOFF', 'in_AFTER'],
        )

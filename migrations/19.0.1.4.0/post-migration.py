import importlib.util
import logging
from pathlib import Path

_logger = logging.getLogger(__name__)


def _load_const_module():
    const_path = Path(__file__).resolve().parents[2] / 'const.py'
    spec = importlib.util.spec_from_file_location('stripe_connector.const', const_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_const = _load_const_module()
LEGACY_STRIPE_ACCOUNT_IDENTIFIER_PATTERN = _const.LEGACY_STRIPE_ACCOUNT_IDENTIFIER_PATTERN


def migrate(cr, version):
    cr.execute(
        """
        SELECT id, name, stripe_account_identifier
          FROM stripe_account
         WHERE stripe_account_identifier ~ %s
         ORDER BY id
        """,
        [LEGACY_STRIPE_ACCOUNT_IDENTIFIER_PATTERN],
    )
    placeholder_identifiers = cr.fetchall()
    if placeholder_identifiers:
        _logger.warning(
            'Legacy placeholder Stripe account IDs are still present after upgrade: %s. '
            'Please replace them with the real Stripe account IDs.',
            ', '.join(
                '%s (%s → %s)' % (name, record_id, identifier)
                for record_id, name, identifier in placeholder_identifiers
            ),
        )

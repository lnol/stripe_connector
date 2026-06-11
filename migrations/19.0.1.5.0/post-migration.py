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


def migrate(cr, version):
    # Fill any remaining NULLs with a recognisable legacy placeholder so the
    # NOT NULL constraint can be applied unconditionally. The 1.4.0 migration
    # (and every subsequent boot) warns about placeholder IDs, prompting admins
    # to replace them with real Stripe account identifiers.
    cr.execute(
        """
        UPDATE stripe_account
           SET stripe_account_identifier = %s || id::text
         WHERE stripe_account_identifier IS NULL
        """,
        [_const.LEGACY_STRIPE_ACCOUNT_IDENTIFIER_PREFIX],
    )
    if cr.rowcount:
        _logger.warning(
            'stripe.account: assigned legacy placeholder Stripe Account IDs to %d record(s) '
            'that had no identifier. Replace them with real Stripe account IDs '
            '(format: acct_XXXXX) via Settings → Stripe Accounts.',
            cr.rowcount,
        )

    cr.execute(
        """
        ALTER TABLE stripe_account
        ALTER COLUMN stripe_account_identifier SET NOT NULL
        """
    )

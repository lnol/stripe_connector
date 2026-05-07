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
LEGACY_STRIPE_ACCOUNT_IDENTIFIER_PREFIX = _const.LEGACY_STRIPE_ACCOUNT_IDENTIFIER_PREFIX


def migrate(cr, version):
    # Odoo applies ``required=True`` schema changes before post-migration
    # hooks run. Legacy databases can still contain NULL / whitespace-only
    # account identifiers, so normalize them here before the registry tries to
    # add the NOT NULL constraint.
    cr.execute(
        """
        UPDATE stripe_account
           SET stripe_account_identifier = NULLIF(BTRIM(stripe_account_identifier), '')
         WHERE stripe_account_identifier IS DISTINCT FROM NULLIF(BTRIM(stripe_account_identifier), '')
        """
    )

    cr.execute(
        """
        WITH updated AS (
            UPDATE stripe_account
               SET stripe_account_identifier = %s || CAST(id AS TEXT)
             WHERE stripe_account_identifier IS NULL
         RETURNING id, name, stripe_account_identifier
        )
        SELECT id, name, stripe_account_identifier
          FROM updated
         ORDER BY id
        """,
        [LEGACY_STRIPE_ACCOUNT_IDENTIFIER_PREFIX],
    )
    missing_identifiers = cr.fetchall()

    if missing_identifiers:
        _logger.warning(
            'Assigned placeholder Stripe account IDs to %d legacy record(s) before schema '
            'upgrade: %s. Please replace them with the real Stripe account IDs.',
            len(missing_identifiers),
            ', '.join(
                '%s (%s → %s)' % (
                    name,
                    record_id,
                    identifier,
                )
                for record_id, name, identifier in missing_identifiers
            ),
        )

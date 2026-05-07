import logging

_logger = logging.getLogger(__name__)


def _legacy_placeholder_stripe_account_identifier(record_id):
    return 'acct_LEGACY%s' % int(record_id)


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
        SELECT id, name
          FROM stripe_account
         WHERE stripe_account_identifier IS NULL
         ORDER BY id
        """
    )
    missing_identifiers = cr.fetchall()
    for record_id, _name in missing_identifiers:
        cr.execute(
            """
            UPDATE stripe_account
               SET stripe_account_identifier = %s
             WHERE id = %s
            """,
            [_legacy_placeholder_stripe_account_identifier(record_id), record_id],
        )

    if missing_identifiers:
        _logger.warning(
            'Assigned placeholder Stripe account IDs to %d legacy record(s) before schema '
            'upgrade: %s. Please replace them with the real Stripe account IDs.',
            len(missing_identifiers),
            ', '.join(
                '%s (%s → %s)' % (
                    name,
                    record_id,
                    _legacy_placeholder_stripe_account_identifier(record_id),
                )
                for record_id, name in missing_identifiers
            ),
        )

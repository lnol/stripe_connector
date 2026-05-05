import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # Older databases can still have the column marked nullable even though
    # the field is required. Normalize legacy whitespace-only values first,
    # then tighten the schema when every record is safe.
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
    if missing_identifiers:
        _logger.warning(
            'stripe.account.stripe_account_identifier is still missing on %d record(s); '
            'leaving the DB column nullable until they are filled: %s',
            len(missing_identifiers),
            ', '.join('%s (%s)' % (name, record_id) for record_id, name in missing_identifiers),
        )
        return

    cr.execute(
        """
        ALTER TABLE stripe_account
        ALTER COLUMN stripe_account_identifier SET NOT NULL
        """
    )

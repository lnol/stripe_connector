import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        SELECT id, name, stripe_account_identifier
          FROM stripe_account
         WHERE stripe_account_identifier LIKE 'acct_LEGACY%%'
         ORDER BY id
        """
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

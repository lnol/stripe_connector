import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.stripe_connector.services.stripe_api import StripeApiService

_logger = logging.getLogger(__name__)


def _backfill_account_identifiers(env):
    accounts = env['stripe.account'].with_context(active_test=False).search([])
    for account in accounts.filtered(lambda rec: not rec.stripe_account_identifier):
        _logger.info('Fetching Stripe account ID for account %s during upgrade', account.display_name)
        payload = StripeApiService(account.api_key).get_account()
        account_identifier = (payload or {}).get('id')
        if not account_identifier:
            raise ValueError(
                'Could not determine Stripe account ID for account %s during upgrade.'
                % account.display_name
            )
        account.write({'stripe_account_identifier': account_identifier})


def _backfill_move_links(env):
    env.cr.execute(
        """
        UPDATE account_move move
           SET stripe_account_id = account.id
          FROM stripe_account account
         WHERE move.stripe_account_id IS NULL
           AND move.stripe_invoice_id IS NOT NULL
           AND move.journal_id = account.sales_journal_id
        """
    )


def _backfill_partner_links(env):
    env.cr.execute(
        """
        UPDATE res_partner partner
           SET stripe_account_id = source.stripe_account_id
          FROM (
                SELECT move.partner_id, MIN(move.stripe_account_id) AS stripe_account_id
                  FROM account_move move
                 WHERE move.partner_id IS NOT NULL
                   AND move.stripe_account_id IS NOT NULL
                 GROUP BY move.partner_id
                HAVING COUNT(DISTINCT move.stripe_account_id) = 1
               ) AS source
         WHERE partner.id = source.partner_id
           AND partner.stripe_customer_id IS NOT NULL
           AND partner.stripe_account_id IS NULL
        """
    )


def _backfill_product_links(env):
    env.cr.execute(
        """
        UPDATE product_template template
           SET stripe_account_id = source.stripe_account_id
          FROM (
                SELECT product.product_tmpl_id, MIN(move.stripe_account_id) AS stripe_account_id
                  FROM account_move_line line
                  JOIN account_move move
                    ON move.id = line.move_id
                  JOIN product_product product
                    ON product.id = line.product_id
                 WHERE move.stripe_account_id IS NOT NULL
                 GROUP BY product.product_tmpl_id
                HAVING COUNT(DISTINCT move.stripe_account_id) = 1
               ) AS source
         WHERE template.id = source.product_tmpl_id
           AND template.stripe_product_id IS NOT NULL
           AND template.stripe_account_id IS NULL
        """
    )


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    _backfill_account_identifiers(env)
    _backfill_move_links(env)
    _backfill_partner_links(env)
    _backfill_product_links(env)

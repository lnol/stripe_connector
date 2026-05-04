from datetime import timedelta

from odoo import SUPERUSER_ID, api, fields


def _stripe_account_model_id(env):
    return env['ir.model']._get_id('stripe.account')


def _upsert_cron_xmlid(env, xmlid, create_vals, update_vals=None):
    """Create or update a cron job identified by its XML ID.

    When the record already exists, only ``update_vals`` is written so that
    administrator customizations to scheduling fields (active, user_id,
    interval_number, interval_type) are preserved.  If ``update_vals`` is
    omitted the full ``create_vals`` dict is used for both paths.
    """
    cron = env.ref(xmlid, raise_if_not_found=False)
    if cron:
        cron.write(update_vals if update_vals is not None else create_vals)
        return cron

    cron = env['ir.cron'].create(create_vals)
    module, name = xmlid.split('.', 1)
    env['ir.model.data'].create({
        'module': module,
        'name': name,
        'model': 'ir.cron',
        'res_id': cron.id,
        'noupdate': True,
    })
    return cron


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    model_id = _stripe_account_model_id(env)
    nextcall = fields.Datetime.now() + timedelta(days=1)

    # Update the existing daily-scheduler cron.  Only the name and code are
    # changed here; active/user_id/interval settings are left untouched so
    # that administrator customizations (e.g. a disabled cron or a custom
    # schedule) survive the upgrade.
    _upsert_cron_xmlid(
        env,
        'stripe_connector.cron_stripe_fetch_invoices',
        create_vals={
            'name': 'Stripe: Queue Invoice Fetches',
            'model_id': model_id,
            'state': 'code',
            'code': 'model._cron_queue_all_fetches()',
            'active': True,
            'user_id': env.ref('base.user_root').id,
            'interval_number': 1,
            'interval_type': 'days',
        },
        update_vals={
            'name': 'Stripe: Queue Invoice Fetches',
            'model_id': model_id,
            'state': 'code',
            'code': 'model._cron_queue_all_fetches()',
        },
    )
    # This cron is new in this version, so it will always be created with the
    # full set of defaults.
    _upsert_cron_xmlid(
        env,
        'stripe_connector.cron_stripe_process_fetch_queue',
        create_vals={
            'name': 'Stripe: Process Invoice Fetch Queue',
            'model_id': model_id,
            'state': 'code',
            'code': 'model._cron_process_fetch_queue()',
            'active': True,
            'user_id': env.ref('base.user_root').id,
            'interval_number': 1,
            'interval_type': 'days',
            'nextcall': nextcall,
        },
    )

from datetime import timedelta

from odoo import SUPERUSER_ID, api, fields


def _stripe_account_model_id(env):
    return env['ir.model']._get_id('stripe.account')


def _upsert_cron_xmlid(env, xmlid, vals):
    cron = env.ref(xmlid, raise_if_not_found=False)
    if cron:
        cron.write(vals)
        return cron

    cron = env['ir.cron'].create(vals)
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

    _upsert_cron_xmlid(env, 'stripe_connector.cron_stripe_fetch_invoices', {
        'name': 'Stripe: Queue Invoice Fetches',
        'model_id': model_id,
        'state': 'code',
        'code': 'model._cron_queue_all_fetches()',
        'active': True,
        'user_id': env.ref('base.user_root').id,
        'interval_number': 1,
        'interval_type': 'days',
    })
    _upsert_cron_xmlid(env, 'stripe_connector.cron_stripe_process_fetch_queue', {
        'name': 'Stripe: Process Invoice Fetch Queue',
        'model_id': model_id,
        'state': 'code',
        'code': 'model._cron_process_fetch_queue()',
        'active': True,
        'user_id': env.ref('base.user_root').id,
        'interval_number': 1,
        'interval_type': 'days',
        'nextcall': nextcall,
    })

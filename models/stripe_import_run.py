from odoo import api, fields, models


class StripeImportRun(models.Model):
    _name = 'stripe.import.run'
    _description = 'Stripe Import Run'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'started_at desc, id desc'

    name = fields.Char(
        string='Reference',
        compute='_compute_name',
        store=True,
        help='Display reference for the Stripe import run.',
    )
    stripe_account_id = fields.Many2one(
        comodel_name='stripe.account',
        string='Stripe Account',
        required=True,
        ondelete='cascade',
        index=True,
        help='Stripe account configuration used for this import run.',
    )
    company_id = fields.Many2one(
        related='stripe_account_id.company_id',
        store=True,
        readonly=True,
        help='Company that owns this import run.',
    )
    state = fields.Selection(
        selection=[
            ('running', 'Running'),
            ('done', 'Done'),
            ('partial', 'Partial'),
            ('failed', 'Failed'),
        ],
        string='Status',
        default='running',
        readonly=True,
        tracking=True,
        copy=False,
        help='Current status of the Stripe import run.',
    )
    started_at = fields.Datetime(
        string='Started At',
        default=fields.Datetime.now,
        readonly=True,
        copy=False,
        help='Date and time when this import run started.',
    )
    finished_at = fields.Datetime(
        string='Finished At',
        readonly=True,
        copy=False,
        help='Date and time when this import run finished.',
    )
    invoice_count = fields.Integer(
        string='Invoices',
        readonly=True,
        help='Number of Stripe invoices seen during this import run.',
    )
    credit_note_count = fields.Integer(
        string='Credit Notes',
        readonly=True,
        help='Number of Stripe credit notes seen during this import run.',
    )
    skipped_count = fields.Integer(
        string='Skipped',
        readonly=True,
        help='Number of Stripe objects skipped during this import run.',
    )
    error_count = fields.Integer(
        string='Errors',
        readonly=True,
        help='Number of Stripe objects that failed during this import run.',
    )
    message = fields.Text(
        string='Summary',
        readonly=True,
        help='Summary of the Stripe import run result.',
    )
    line_ids = fields.One2many(
        comodel_name='stripe.import.run.line',
        inverse_name='run_id',
        string='Import Lines',
        help='Per-object results for this Stripe import run.',
    )

    @api.depends('stripe_account_id', 'started_at')
    def _compute_name(self):
        for run in self:
            started_at = fields.Datetime.to_string(run.started_at) if run.started_at else ''
            account_name = run.stripe_account_id.display_name or 'Stripe'
            run.name = '%s - %s' % (account_name, started_at)


class StripeImportRunLine(models.Model):
    _name = 'stripe.import.run.line'
    _description = 'Stripe Import Run Line'
    _order = 'run_id desc, id'

    run_id = fields.Many2one(
        comodel_name='stripe.import.run',
        string='Import Run',
        required=True,
        ondelete='cascade',
        index=True,
        help='Import run that produced this line.',
    )
    stripe_account_id = fields.Many2one(
        related='run_id.stripe_account_id',
        store=True,
        readonly=True,
        help='Stripe account configuration used for this import line.',
    )
    company_id = fields.Many2one(
        related='run_id.company_id',
        store=True,
        readonly=True,
        help='Company that owns this import line.',
    )
    stripe_object_type = fields.Selection(
        selection=[
            ('invoice', 'Invoice'),
            ('credit_note', 'Credit Note'),
        ],
        string='Stripe Object Type',
        required=True,
        readonly=True,
        help='Type of Stripe object processed for this import line.',
    )
    stripe_object_id = fields.Char(
        string='Stripe Object ID',
        readonly=True,
        index=True,
        help='Identifier of the Stripe object processed for this import line.',
    )
    state = fields.Selection(
        selection=[
            ('done', 'Done'),
            ('skipped', 'Skipped'),
            ('failed', 'Failed'),
        ],
        string='Status',
        required=True,
        readonly=True,
        help='Processing result for this Stripe object.',
    )
    move_id = fields.Many2one(
        comodel_name='account.move',
        string='Journal Entry',
        readonly=True,
        ondelete='set null',
        help='Odoo journal entry created or found for this Stripe object.',
    )
    message = fields.Text(
        string='Message',
        readonly=True,
        help='Detailed processing message for this Stripe object.',
    )

def migrate(cr, version):
    # No automatic backfill is performed. After upgrading, the user must
    # manually set the stripe_account_identifier on each stripe.account record.
    pass

STRIPE_API_BASE_URL = 'https://api.stripe.com/v1/'
STRIPE_REQUEST_TIMEOUT = (5, 30)
LEGACY_STRIPE_ACCOUNT_IDENTIFIER_PREFIX = 'acct_LEGACY'
LEGACY_STRIPE_ACCOUNT_IDENTIFIER_PATTERN = r'^acct_LEGACY\d+$'


def legacy_placeholder_stripe_account_identifier(record_id):
    return '%s%s' % (LEGACY_STRIPE_ACCOUNT_IDENTIFIER_PREFIX, int(record_id))

with source as (
    select * from {{ source('product', 'raw_usage_events') }}
)


select event_id,
    event_type,
    lower(account_domain) as account_domain,
    lower(user_email) as user_email,
    credits_used,
    occurred_at,
    ingested_at
from source
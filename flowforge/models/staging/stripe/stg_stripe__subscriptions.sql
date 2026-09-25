with source as (
    select * from {{ source('stripe', 'raw_stripe_subscriptions') }}
),

parsed as (
    select
        id as subscription_id,
        json_value(raw_json, '$.customer') as customer_id,
        json_value(raw_json, '$.status') as subscription_status,
        json_value(raw_json, '$.currency') as currency,
        json_value(item, '$.price.id') as price_id,
        safe_cast(json_value(item, '$.price.unit_amount') as int64) / 100 as monthly_amount_usd,
        safe_cast(json_value(item, '$.quantity') as int64) as quantity,
        timestamp_seconds(safe_cast(json_value(item, '$.current_period_start') as int64)) as current_period_start,
        timestamp_seconds(safe_cast(json_value(item, '$.current_period_end') as int64)) as current_period_end,
        timestamp_seconds(safe_cast(json_value(raw_json, '$.created') as int64)) as created_at,
        timestamp_seconds(safe_cast(json_value(raw_json, '$.canceled_at') as int64)) as canceled_at,
        timestamp_seconds(safe_cast(json_value(raw_json, '$.ended_at') as int64)) as ended_at,
        safe_cast(json_value(raw_json, '$.cancel_at_period_end') as bool) as cancel_at_period_end,
        extracted_at
    from source
    left join unnest(json_query_array(raw_json, '$.items.data')) as item
)

select * from parsed
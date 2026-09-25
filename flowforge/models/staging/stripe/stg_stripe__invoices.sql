with source as (
    select * from {{ source('stripe', 'raw_stripe_invoices') }}
),

parsed as (
    select
        id as invoice_id,
        json_value(raw_json, '$.customer') as customer_id,
        json_value(raw_json, '$.parent.subscription_details.subscription') as subscription_id,
        json_value(raw_json, '$.status') as payment_status,
        safe_cast(json_value(raw_json, '$.amount_due') as INT64) / 100 as amount_due_usd,
        safe_cast(json_value(raw_json, '$.amount_paid') as INT64) / 100 as amount_paid_usd,
        json_value(raw_json, '$.currency') as currency,
        TIMESTAMP_SECONDS(CAST(JSON_VALUE(raw_json, '$.created') AS INT64)) as created_at,
        extracted_at
    from source
)

select * from parsed
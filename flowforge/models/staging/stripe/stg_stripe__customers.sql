with source as (
    select * from {{ source('stripe', 'raw_stripe_customers') }}
),

parsed as (
    select
        id as customer_id,
        json_value(raw_json, '$.name') as customer_name,
        lower(json_value(raw_json, '$.email')) as email,
        json_value(raw_json, '$.metadata.hubspot_company_id')  as hubspot_company_id,
        TIMESTAMP_SECONDS(CAST(JSON_VALUE(raw_json, '$.created') AS INT64)) as created_at,
        extracted_at
    from source
)

select * from parsed
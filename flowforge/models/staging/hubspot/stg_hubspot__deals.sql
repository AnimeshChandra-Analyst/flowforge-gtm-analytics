with source as (
    select * from {{ source('hubspot', 'raw_hubspot_deals') }}
),

parsed as (
    select
        id as deal_id,
        json_value(raw_json, '$.properties.dealname') as deal_name,
        json_value(raw_json, '$.properties.dealstage') as deal_stage,
        json_value(raw_json, '$.properties.pipeline') as pipeline,
        safe_cast(json_value(raw_json, '$.properties.amount') as numeric) as amount,
        safe_cast(json_value(raw_json, '$.properties.closedate') as timestamp) as close_date,
        safe_cast(json_value(raw_json, '$.properties.createdate') as timestamp) as created_at,
        safe_cast(json_value(raw_json, '$.properties.hs_lastmodifieddate') as timestamp) as updated_at,
        extracted_at
    from source
)

select * from parsed
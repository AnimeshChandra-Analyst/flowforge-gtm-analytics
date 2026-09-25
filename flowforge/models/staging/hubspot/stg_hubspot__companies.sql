with source as (
    select * from {{ source('hubspot', 'raw_hubspot_companies') }}
),

parsed as (
    select
        id as company_id,
        json_value(raw_json, '$.properties.name') as company_name,
        lower(json_value(raw_json, '$.properties.domain')) as domain,
        safe_cast(json_value(raw_json, '$.properties.numberofemployees') as int64) as employees,
        safe_cast(json_value(raw_json, '$.properties.createdate') as timestamp) as created_at,
        extracted_at
    from source
)

select * from parsed
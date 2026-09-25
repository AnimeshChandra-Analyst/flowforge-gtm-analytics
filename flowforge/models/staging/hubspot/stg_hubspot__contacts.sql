with source as (
    select * from {{ source('hubspot', 'raw_hubspot_contacts') }}
),

parsed as (
    select
        id as contact_id,
        lower(json_value(raw_json, '$.properties.email')) as email,
        split(lower(json_value(raw_json, '$.properties.email')), '@')[safe_offset(1)] as email_domain,
        json_value(raw_json, '$.properties.firstname') as first_name,
        json_value(raw_json, '$.properties.lastname') as last_name,
        json_value(raw_json, '$.properties.company') as company_name,
        safe_cast(json_value(raw_json, '$.properties.createdate') as timestamp) as created_at,
        safe_cast(json_value(raw_json, '$.properties.lastmodifieddate') as timestamp) as updated_at,
        extracted_at
    from source
)

select * from parsed
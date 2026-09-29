with hubspot_companies as (
    select * from {{ ref('stg_hubspot__companies') }}
),

stripe_customers as (
    select * from {{ ref('stg_stripe__customers') }}
),

direct_match as (
    select
        h.company_id as hubspot_company_id,
        h.company_name,
        h.domain as company_domain,
        h.employees as total_employees,
        s.customer_id,
        'direct' as match_type
    from hubspot_companies h
    inner join stripe_customers s
        on h.company_id = s.hubspot_company_id
),

name_match as (
    select
        h.company_id as hubspot_company_id,
        h.company_name,
        h.domain as company_domain,
        h.employees as total_employees,
        s.customer_id,
        'name' as match_type
    from hubspot_companies h
    inner join stripe_customers s
        on h.company_name = s.customer_name
    where h.company_id not in (select hubspot_company_id from direct_match)
),

--  Whatever is left has no billing record we can find at all.
unmatched as (
    select
        h.company_id as hubspot_company_id,
        h.company_name,
        h.domain as company_domain,
        h.employees as total_employees,
        cast(null as string) as customer_id,
        'unmatched' as match_type
    from hubspot_companies h
    where h.company_id not in (select hubspot_company_id from direct_match)
      and h.company_id not in (select hubspot_company_id from name_match)
),

combined as (
    select * from direct_match
    union all
    select * from name_match
    union all
    select * from unmatched
)

select * from combined
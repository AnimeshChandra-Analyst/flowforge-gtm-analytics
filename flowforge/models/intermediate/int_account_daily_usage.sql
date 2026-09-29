with usage_events as (
    select * from {{ ref('stg_product__usage_events') }}
),

account_identity as (
    select * from {{ ref('int_account_identity') }}
),

account_usage_summary as (
    select
        a.hubspot_company_id,
        u.account_domain,
        date(u.occurred_at) as usage_date,
        sum(u.credits_used) as total_credits_used,
        count(distinct u.user_email) as active_users,
        countif(u.event_type = 'session_started') as sessions,
        countif(event_type = 'prompt_run') as prompt_runs,
        countif(event_type = 'project_created') as projects_created,
        countif(event_type = 'teammate_invited') as teammates_invited,
        countif(event_type = 'project_published') as projects_published,
        count(*) as total_events
    from usage_events u
    inner join account_identity a                                               -- Inner join on purpose: events with no account_domain (~3%, planted)
        on u.account_domain = a.company_domain                                  -- can't be attributed to a company, so they're dropped here.
    group by a.hubspot_company_id, u.account_domain, date(u.occurred_at)

)


select * from account_usage_summary
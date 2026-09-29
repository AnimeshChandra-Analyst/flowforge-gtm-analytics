-- Product Qualified Leads: accounts whose behaviour says they're ready to
-- talk to sales, so reps don't have to guess who to call.
--
-- Rule: 50+ employees AND at least 2 of these 3 signals in the last 14 days
--   - 3 or more active users
--   - used 80% or more of their monthly credits
--   - invited a new teammate


with account_identity as (
    select * from {{ ref('int_account_identity') }}
),

account_subscriptions as (
    select * from {{ ref('int_account_subscription') }}
),

daily_usage as (
    select * from {{ ref('int_account_daily_usage') }}
),

-- What each account did over the last 14 days.
recent_usage as (
    select
        hubspot_company_id,
        sum(total_credits_used) as credits_used,
        max(active_users) as peak_active_users,
        sum(teammates_invited) as teammates_invited,
        max(usage_date) as last_active_date
    from daily_usage
    where usage_date >= current_date() - 14
    group by hubspot_company_id
),

-- Turn each signal into a 1 or a 0 so they can simply be added up.
signals as (
    select
        i.hubspot_company_id,
        i.company_name,
        i.company_domain,
        i.total_employees,
        s.plan_name,
        s.monthly_credits,
        s.monthly_amount_usd,
        coalesce(u.credits_used, 0) as credits_used_14d,
        coalesce(u.peak_active_users, 0) as active_users,
        coalesce(u.teammates_invited, 0) as teammates_invited_14d,
        u.last_active_date,

        -- Signal 1: it's spreading beyond one person.
        if(coalesce(u.peak_active_users, 0) >= 3, 1, 0) as signal_active_users,

        -- Signal 2: they're about to hit a wall on credits.
        -- safe_divide returns null rather than erroring if the limit is 0.
        if(
            coalesce(safe_divide(u.credits_used, s.monthly_credits), 0) >= 0.8,
            1, 0
        ) as signal_credit_usage,

        -- Signal 3: someone is actively pulling colleagues in.
        if(coalesce(u.teammates_invited, 0) >= 1, 1, 0) as signal_invite

    from account_identity i
    inner join account_subscriptions s
        on i.hubspot_company_id = s.hubspot_company_id
    left join recent_usage u
        on i.hubspot_company_id = u.hubspot_company_id
    where
        i.total_employees >= 50
        -- Only accounts with room to grow into an Enterprise contract.
        and s.plan_name in ('free', 'pro', 'team')
        and s.is_active
),

scored as (
    select
        *,
        signal_active_users + signal_credit_usage + signal_invite as signal_count,
        round(safe_divide(credits_used_14d, monthly_credits) * 100, 1) as credit_usage_pct
    from signals
)

select *
from scored
where signal_count >= 2
order by signal_count desc, credits_used_14d desc
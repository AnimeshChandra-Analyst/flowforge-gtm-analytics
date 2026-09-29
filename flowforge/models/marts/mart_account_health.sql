-- One row per paying account, with a red / yellow / green health status.
--
-- Health compares credits used in the last 14 days against the 14 days
-- before that. 

with account_subscriptions as (
    select * from {{ ref('int_account_subscription') }}
),

daily_usage as (
    select * from {{ ref('int_account_daily_usage') }}
),

-- Two equal 14 day windows, side by side on one row per account.

usage_window as (
    select
        u.hubspot_company_id,
        sum(
            case
                when u.usage_date >= current_date() - 14
                then u.total_credits_used
                else 0
            end
        ) as credits_recent,
        sum(
            case
                when u.usage_date >= current_date() - 28
                 and u.usage_date < current_date() - 14
                then u.total_credits_used
                else 0
            end
        ) as credits_previous
    from daily_usage u
    group by u.hubspot_company_id
),


with_usage as (
    select
        s.hubspot_company_id,
        s.company_name,
        s.plan_name,
        s.monthly_amount_usd,
        s.subscription_status,
        s.is_active,
        coalesce(w.credits_recent, 0) as credits_recent,
        coalesce(w.credits_previous, 0) as credits_previous
    from account_subscriptions s
    left join usage_window w
        on s.hubspot_company_id = w.hubspot_company_id
    -- Health is about paying customers. A free account going quiet
    -- isn't revenue at risk, and they'd flood the list.
    where s.is_active
      and s.plan_name != 'free'
),

account_health as (
    select
        *,
        -- Order matters: red is checked first, so an account that dropped
        -- 60% is red rather than falling through to the yellow rule.
        case
            when credits_recent = 0
              or credits_recent < credits_previous * 0.5
            then 'red'
            when credits_recent < credits_previous * 0.75
            then 'yellow'
            else 'green'
        end as health_status
    from with_usage
)

select * from account_health
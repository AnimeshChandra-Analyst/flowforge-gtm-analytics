-- One row per account, with everything about them in one place.
--
-- This is the table the app reads. A sales or CS person looking up an
-- account wants the plan, what they pay, how much they're using it,
-- whether they're at risk and whether they're worth calling, without
-- having to join five tables to find out.
--
-- It's also the easiest table to aggregate, so plan-level and
-- company-wide summaries can be built from it rather than duplicating
-- the logic in another model.

with identity as (
    select * from {{ ref('int_account_identity') }}
),

subscriptions as (
    select * from {{ ref('int_account_subscription') }}
),

daily_usage as (
    select * from {{ ref('int_account_daily_usage') }}
),

health as (
    select * from {{ ref('mart_account_health') }}
),

pqls as (
    select * from {{ ref('mart_product_qualified_leads') }}
),

invoices as (
    select * from {{ ref('stg_stripe__invoices') }}
),

account_contact as (
    select * from {{ ref('stg_hubspot__contacts') }}
),

-- Usage over the last 14 days, and how that compares to the 14 before.
usage_windows as (
    select
        hubspot_company_id,
        sum(case when usage_date >= current_date() - 14 then total_credits_used else 0 end) as credits_used_14d,
        sum(case when usage_date >= current_date() - 28
                  and usage_date < current_date() - 14 then total_credits_used else 0 end) as credits_previous_14d,
        sum(case when usage_date >= current_date() - 14 then prompt_runs else 0 end) as prompt_runs_14d,
        sum(case when usage_date >= current_date() - 14 then sessions else 0 end) as sessions_14d,
        sum(case when usage_date >= current_date() - 14 then teammates_invited else 0 end) as teammates_invited_14d,
        max(case when usage_date >= current_date() - 14 then active_users else 0 end) as active_users_14d,
        max(usage_date) as last_active_date,
        sum(total_credits_used) as credits_used_all_time
    from daily_usage
    group by hubspot_company_id
),

-- What each account has actually paid us to date.
lifetime_revenue as (
    select
        customer_id,
        sum(amount_paid_usd) as lifetime_revenue_usd,
        count(*) as invoices_paid
    from invoices
    where payment_status = 'paid'
    group by customer_id
),

contacts_ranked as (
    select
        email,
        email_domain,
        concat(first_name, ' ', last_name) as contact_name,
        row_number() over (
            partition by email_domain
            order by created_at asc
        ) as rn
    from account_contact
),

primary_contact as (
    select email, email_domain, contact_name
    from contacts_ranked
    where rn = 1
),

combined as (
    select
        i.hubspot_company_id,
        i.company_name,
        i.company_domain,
        i.total_employees,
        i.match_type as billing_match_type,

        -- Subscription
        s.customer_id,
        s.subscription_id,
        s.plan_name,
        s.plan_rank,
        s.monthly_amount_usd,
        case when s.is_active then s.monthly_amount_usd else 0 end as current_mrr_usd,
        s.monthly_amount_usd * 12 as annual_run_rate_usd,
        s.subscription_status,
        s.is_active,
        s.created_at as subscription_started_at,
        s.canceled_at,
        s.seat_limit,
        s.monthly_credits,
        date_diff(current_date(), date(s.created_at), day) as tenure_days,

        -- Usage
        coalesce(u.credits_used_14d, 0) as credits_used_14d,
        coalesce(u.credits_previous_14d, 0) as credits_previous_14d,
        coalesce(u.prompt_runs_14d, 0) as prompt_runs_14d,
        coalesce(u.sessions_14d, 0) as sessions_14d,
        coalesce(u.teammates_invited_14d, 0) as teammates_invited_14d,
        coalesce(u.active_users_14d, 0) as active_users_14d,
        coalesce(u.credits_used_all_time, 0) as credits_used_all_time,
        u.last_active_date,
        date_diff(current_date(), u.last_active_date, day) as days_since_last_active,

        -- How close they are to their limits. This is what drives upgrades.
        round(safe_divide(u.credits_used_14d, s.monthly_credits) * 100, 1) as credit_usage_pct,
        round(safe_divide(u.active_users_14d, s.seat_limit) * 100, 1) as seat_usage_pct,

        -- Money
        coalesce(r.lifetime_revenue_usd, 0) as lifetime_revenue_usd,
        coalesce(r.invoices_paid, 0) as invoices_paid,

        -- Flags from the other marts. Free accounts have no health status,
        -- so they get 'not_scored' rather than a null nobody can interpret.
        coalesce(h.health_status, 'not_scored') as health_status,
        p.hubspot_company_id is not null as is_pql,
        coalesce(p.signal_count, 0) as pql_signal_count,

        -- contact info
        c.email as contact_email,
        c.contact_name

    from identity i
    left join subscriptions s on i.hubspot_company_id = s.hubspot_company_id
    left join usage_windows u on i.hubspot_company_id = u.hubspot_company_id
    left join lifetime_revenue r on s.customer_id = r.customer_id
    left join health h on i.hubspot_company_id = h.hubspot_company_id
    left join pqls p on i.hubspot_company_id = p.hubspot_company_id
    left join primary_contact c on i.company_domain = c.email_domain
),

final as (
    select
        *,
        -- A single label that answers "what should we do about this account?"
        case
            when not is_active then 'churned'
            when is_pql then 'sales opportunity'
            when health_status = 'red' then 'at risk'
            when health_status = 'yellow' then 'watch'
            when plan_name = 'free' and credits_used_14d = 0 then 'dormant free'
            when plan_name = 'free' then 'active free'
            else 'healthy paying'
        end as account_segment,
        current_timestamp() as refreshed_at
    from combined
)

select * from final
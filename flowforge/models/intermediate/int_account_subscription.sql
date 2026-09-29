with stripe_subscriptions as (
    select * from {{ ref('stg_stripe__subscriptions') }}
),

account_identity as (
    select * from {{ ref('int_account_identity') }}
),

plans as (
    select * from {{ ref('plans') }}
),

subscription_summary as(
    select
       a.hubspot_company_id,
       a.customer_id,
       a.company_name,
       s.subscription_id,
       p.plan_name,
       p.plan_rank,
       p.seat_limit,
       p.monthly_credits,
       s.monthly_amount_usd,
       s.subscription_status,
       s.created_at,
       s.canceled_at,
       s.subscription_status = 'active' as is_active
    from account_identity a 
    left join stripe_subscriptions s
    on a.customer_id = s.customer_id
    left join plans p
    on s.price_id = p.price_id
)

select * from subscription_summary
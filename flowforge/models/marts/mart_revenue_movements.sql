-- Weekly revenue movements: where did our MRR change actually come from?
--
--   new          accounts that started paying (including free -> paid)
--   expansion    paying accounts that upgraded
--   contraction  paying accounts that downgraded but still pay
--   churned      paying accounts that stopped paying (cancelled, or
--                downgraded to free)
--
-- The four movements always reconcile: summing them over all weeks gives
-- today's total MRR. If ending_mrr for the latest week doesn't equal
-- sum(current_mrr_usd) in mart_account_overview, something is wrong.
--
-- An earlier version counted new MRR at each subscription's CURRENT price,
-- then counted upgrades again as expansion, which double counted every
-- upgrade. New MRR now uses the price the subscription had the first time
-- the snapshot saw it.

with subscriptions as (
    select * from {{ ref('int_account_subscription') }}
),

snapshot_history as (
    select * from {{ ref('snap_subscriptions') }}
),

-- The price each subscription had the first time we recorded it.
first_seen_ranked as (
    select
        subscription_id,
        monthly_amount_usd,
        row_number() over (
            partition by subscription_id
            order by dbt_valid_from
        ) as rn
    from snapshot_history
),

first_seen as (
    select subscription_id, monthly_amount_usd as starting_amount
    from first_seen_ranked
    where rn = 1
),

-- Every subscription start, at its starting price. Subscriptions created
-- since the last snapshot run aren't in it yet, so fall back to current.
starts as (
    select
        date_trunc(date(s.created_at), week(monday)) as week_start,
        coalesce(f.starting_amount, s.monthly_amount_usd) as amount
    from subscriptions s
    left join first_seen f
        on s.subscription_id = f.subscription_id
),

-- Every price change the snapshot recorded, next to the price before it.
-- lag() looks back one row within each subscription's history.
price_changes as (
    select
        date_trunc(date(dbt_valid_from), week(monday)) as week_start,
        monthly_amount_usd as new_amount,
        lag(monthly_amount_usd) over (
            partition by subscription_id
            order by dbt_valid_from
        ) as previous_amount
    from snapshot_history
),

-- One row per movement, all in the same shape so they can be stacked.
movements as (

    -- Subscriptions that started out paying
    select
        week_start,
        amount as new_mrr,
        0.0 as expansion_mrr,
        0.0 as contraction_mrr,
        0.0 as churned_mrr,
        1 as new_accounts,
        0 as upgrades,
        0 as downgrades,
        0 as churned_accounts
    from starts
    where amount > 0

    union all

    -- Plan changes, classified by what they did to revenue
    select
        week_start,
        case when previous_amount = 0 and new_amount > 0
             then new_amount else 0 end,
        case when previous_amount > 0 and new_amount > previous_amount
             then new_amount - previous_amount else 0 end,
        case when new_amount > 0 and new_amount < previous_amount
             then previous_amount - new_amount else 0 end,
        case when previous_amount > 0 and new_amount = 0
             then previous_amount else 0 end,
        case when previous_amount = 0 and new_amount > 0 then 1 else 0 end,
        case when previous_amount > 0 and new_amount > previous_amount then 1 else 0 end,
        case when new_amount > 0 and new_amount < previous_amount then 1 else 0 end,
        case when previous_amount > 0 and new_amount = 0 then 1 else 0 end
    from price_changes
    where previous_amount is not null
      and new_amount != previous_amount

    union all

    -- Cancellations. The price stays on a cancelled subscription, so this
    -- is what they were paying when they left.
    select
        date_trunc(date(canceled_at), week(monday)),
        0.0,
        0.0,
        0.0,
        monthly_amount_usd,
        0,
        0,
        0,
        1
    from subscriptions
    where canceled_at is not null
      and monthly_amount_usd > 0
),

-- Every week from the first subscription to now, so quiet weeks still
-- show as zero rather than disappearing.
week_spine as (
    select week_start
    from unnest(
        generate_date_array(
            (select min(week_start) from starts),
            date_trunc(current_date(), week(monday)),
            interval 1 week
        )
    ) as week_start
),

weekly as (
    select
        w.week_start,
        coalesce(sum(m.new_mrr), 0) as new_mrr,
        coalesce(sum(m.expansion_mrr), 0) as expansion_mrr,
        coalesce(sum(m.contraction_mrr), 0) as contraction_mrr,
        coalesce(sum(m.churned_mrr), 0) as churned_mrr,
        coalesce(sum(m.new_accounts), 0) as new_accounts,
        coalesce(sum(m.upgrades), 0) as upgrades,
        coalesce(sum(m.downgrades), 0) as downgrades,
        coalesce(sum(m.churned_accounts), 0) as churned_accounts
    from week_spine w
    left join movements m
        on w.week_start = m.week_start
    group by w.week_start
),

final as (
    select
        *,
        new_mrr + expansion_mrr - contraction_mrr - churned_mrr as net_mrr_change,
        sum(new_mrr + expansion_mrr - contraction_mrr - churned_mrr)
            over (order by week_start) as ending_mrr
    from weekly
)

select * from final
order by week_start
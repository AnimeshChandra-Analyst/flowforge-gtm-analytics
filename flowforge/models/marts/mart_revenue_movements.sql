-- Weekly revenue movements: where did our MRR change actually come from?
--
-- Total MRR going up by $200 hides the interesting part. Growing because
-- of new customers is a very different story from growing while quietly
-- losing existing ones. These four numbers always add up to the net change.
--
--   new          accounts that started paying
--   expansion    existing accounts that upgraded
--   contraction  existing accounts that downgraded
--   churned      accounts that stopped paying
--
-- New and churned come from Stripe's own created_at and canceled_at dates,
-- so they have history from day one. Expansion and contraction can only
-- come from the snapshot, because the raw tables are overwritten daily and
-- an account's previous price is gone. That means those two columns stay
-- empty until the daily job has been running for a while. Expected, not a bug.

with subscriptions as (
    select * from {{ ref('int_account_subscription') }}
),

snapshot_history as (
    select * from {{ ref('snap_subscriptions') }}
),

-- Every week from the first subscription to now, so weeks with no
-- activity still show up as a zero row rather than disappearing.
week_spine as (
    select week_start
    from unnest(
        generate_date_array(
            (select date_trunc(min(date(created_at)), week(monday)) from subscriptions),
            date_trunc(current_date(), week(monday)),
            interval 1 week
        )
    ) as week_start
),

-- Accounts that started paying, by the week they started.
new_revenue as (
    select
        date_trunc(date(created_at), week(monday)) as week_start,
        sum(monthly_amount_usd) as new_mrr,
        count(*) as new_accounts
    from subscriptions
    where monthly_amount_usd > 0
    group by 1
),

-- Accounts that stopped paying, by the week they cancelled.
churned_revenue as (
    select
        date_trunc(date(canceled_at), week(monday)) as week_start,
        sum(monthly_amount_usd) as churned_mrr,
        count(*) as churned_accounts
    from subscriptions
    where canceled_at is not null
      and monthly_amount_usd > 0
    group by 1
),

-- Plan changes, found by comparing each snapshot row to the one before it
-- for the same subscription. lag() looks back one row within a partition.
plan_changes as (
    select
        subscription_id,
        date(dbt_valid_from) as change_date,
        monthly_amount_usd as new_amount,
        lag(monthly_amount_usd) over (
            partition by subscription_id
            order by dbt_valid_from
        ) as previous_amount
    from snapshot_history
),

movements as (
    select
        date_trunc(change_date, week(monday)) as week_start,
        sum(
            case
                when new_amount > previous_amount
                then new_amount - previous_amount
                else 0
            end
        ) as expansion_mrr,
        sum(
            case
                when new_amount < previous_amount
                then previous_amount - new_amount
                else 0
            end
        ) as contraction_mrr,
        countif(new_amount > previous_amount) as upgrades,
        countif(new_amount < previous_amount) as downgrades
    from plan_changes
    where previous_amount is not null   -- first row per subscription isn't a change
      and new_amount != previous_amount
    group by 1
),

combined as (
    select
        w.week_start,
        coalesce(n.new_mrr, 0) as new_mrr,
        coalesce(m.expansion_mrr, 0) as expansion_mrr,
        coalesce(m.contraction_mrr, 0) as contraction_mrr,
        coalesce(c.churned_mrr, 0) as churned_mrr,
        coalesce(n.new_accounts, 0) as new_accounts,
        coalesce(m.upgrades, 0) as upgrades,
        coalesce(m.downgrades, 0) as downgrades,
        coalesce(c.churned_accounts, 0) as churned_accounts
    from week_spine w
    left join new_revenue n on w.week_start = n.week_start
    left join churned_revenue c on w.week_start = c.week_start
    left join movements m on w.week_start = m.week_start
),

final as (
    select
        *,
        -- The four movements always reconcile to the net change.
        new_mrr + expansion_mrr - contraction_mrr - churned_mrr as net_mrr_change,
        -- Running total gives you the MRR line chart.
        sum(new_mrr + expansion_mrr - contraction_mrr - churned_mrr)
            over (order by week_start) as ending_mrr
    from combined
)

select * from final
order by week_start
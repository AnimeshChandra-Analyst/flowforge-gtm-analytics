-- Fails if the Revenue page and the Accounts page disagree on total MRR.
-- They must always match: ending MRR is just every movement added up.
-- A dbt test passes when this query returns no rows.

with latest_revenue as (
    select ending_mrr
    from {{ ref('mart_revenue_movements') }}
    order by week_start desc
    limit 1
),

current_mrr as (
    select sum(current_mrr_usd) as total_mrr
    from {{ ref('mart_account_overview') }}
)

select
    r.ending_mrr,
    c.total_mrr,
    r.ending_mrr - c.total_mrr as gap
from latest_revenue r
cross join current_mrr c
where r.ending_mrr != c.total_mrr
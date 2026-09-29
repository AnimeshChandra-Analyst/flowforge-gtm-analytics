{#
    Records how each subscription changes over time.

    The raw Stripe tables are overwritten on every extract, so they only
    ever hold the CURRENT state of a subscription. If an account moves from
    Pro to Team, the fact they were once on Pro disappears.

    This snapshot keeps that history: when something we care about changes,
    the old row is closed off with a valid_to date and a new row is opened.
    Thats what makes "what was this account paying last week?" answerable,
    which revenue movements depends on.

    Runs with `dbt snapshot`, not `dbt run`, and must run BEFORE the models.
#}

{% snapshot snap_subscriptions %}

{{
    config(
        target_schema='snapshots',
        unique_key='subscription_id',

        strategy='check',
        check_cols=[
            'plan_name',
            'monthly_amount_usd',
            'subscription_status',
            'is_active',
        ],

        invalidate_hard_deletes=True,
    )
}}

select
    subscription_id,
    hubspot_company_id,
    company_name,
    customer_id,
    plan_name,
    plan_rank,
    monthly_amount_usd,
    subscription_status,
    is_active,
    created_at,
    canceled_at
from {{ ref('int_account_subscription') }}
where subscription_id is not null

{% endsnapshot %}
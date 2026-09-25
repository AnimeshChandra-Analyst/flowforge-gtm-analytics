"""
Daily simulator for FlowForge.

Runs once a day and moves the world forward by one day:

  1. every active account generates a day of usage, based on its archetype
  2. accounts that are pushing against their plan limits may upgrade
  3. accounts that have gone quiet may downgrade or cancel
  4. a handful of brand new companies sign up

Plan changes are NOT random. They depend on how much of their plan an
account has actually used in the last 30 days. That's what makes the data
worth modelling: usage genuinely predicts what happens next.

Pass 1 plays the customers only. Once reverse ETL starts creating deals,
we'll add the sales rep behaviour (moving deals through stages).
"""

import os
import random
from datetime import datetime, timedelta, timezone

import pandas as pd
import stripe
from dotenv import load_dotenv
from faker import Faker
from google.cloud import bigquery
from google.oauth2 import service_account
from hubspot import HubSpot

# Reuse the behaviour functions and helpers we already wrote, instead of
# copying them. One definition of how a Ghost behaves, in one place.
from generate_usage import BEHAVIOUR, make_event
from seed_world import (
    ARCHETYPES,
    PLAN_PRICE_ENV,
    create_company,
    create_contact,
    create_stripe_customer,
    make_company_name,
    make_domain,
    pick_archetype,
)

load_dotenv()

# IMPORTANT: seed_world fixes the random seed so its runs are repeatable.
# For a daily job we want the opposite, a different roll every day.
random.seed()
Faker.seed(None)
fake = Faker()

# ── Settings ─────────────────────────────────────────────────────────────────

NEW_SIGNUPS_PER_DAY = (5, 10)

SIM_TABLE = "flowforge_sim.sim_accounts"
EVENTS_TABLE = "flowforge_raw.raw_usage_events"

# What each plan includes. This is what accounts push against.
PLAN_LIMITS = {
    "free": {"seats": 1, "credits": 50},
    "pro": {"seats": 3, "credits": 500},
    "team": {"seats": 10, "credits": 3000},
    "enterprise": {"seats": 50, "credits": 20000},
}

# Self-serve upgrades stop at team. Enterprise only comes from a won deal.
UPGRADE_PATH = {"free": "pro", "pro": "team", "team": None, "enterprise": None}
DOWNGRADE_PATH = {"enterprise": "team", "team": "pro", "pro": "free", "free": None}

# Daily chance of upgrading WHEN an account is pushing against its limits.
# Champions are the most likely, Ghosts never do.
UPGRADE_CHANCE = {
    "champion": 0.30,
    "power_solo": 0.25,
    "skeptic_exec": 0.06,
    "ghost": 0.0,
}

# Daily chance of cancelling WHEN an account has gone quiet on a paid plan.
CANCEL_CHANCE = {
    "champion": 0.004,
    "power_solo": 0.010,
    "skeptic_exec": 0.060,
    "ghost": 0.080,
}

# An account is "pushing against limits" above this share of its credits,
# and "gone quiet" below the low one.
HIGH_USAGE = 0.80
LOW_USAGE = 0.15


# ── Clients ──────────────────────────────────────────────────────────────────

def get_bigquery_client() -> bigquery.Client:
    key_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    credentials = service_account.Credentials.from_service_account_file(key_path)
    return bigquery.Client(credentials=credentials, project=os.environ["BQ_PROJECT"])


# ── Working out where each account stands ────────────────────────────────────

def load_account_state(client: bigquery.Client) -> pd.DataFrame:
    """Every account, plus how much they've used in the last 30 days."""
    query = f"""
        with usage as (
            select
                account_domain,
                sum(credits_used) as credits_30d,
                count(distinct user_email) as active_users,
                max(occurred_at) as last_seen_at
            from `{client.project}.{EVENTS_TABLE}`
            where occurred_at >= timestamp_sub(current_timestamp(), interval 30 day)
              and account_domain is not null
            group by account_domain
        ),

        seats as (
            -- accounts start with one user and gain one per invite
            select
                account_domain,
                1 + countif(event_type = 'teammate_invited') as seats
            from `{client.project}.{EVENTS_TABLE}`
            where account_domain is not null
            group by account_domain
        )

        select
            a.*,
            coalesce(u.credits_30d, 0) as credits_30d,
            coalesce(u.active_users, 0) as active_users,
            coalesce(s.seats, 1) as seats,
            u.last_seen_at
        from `{client.project}.{SIM_TABLE}` a
        left join usage u on a.domain = u.account_domain
        left join seats s on a.domain = s.account_domain
    """
    df = client.query(query).to_dataframe()

    # These columns only exist after the first daily run, so create them
    # the first time round.
    if "current_plan" not in df.columns:
        df["current_plan"] = df["starting_plan"]
    if "status" not in df.columns:
        df["status"] = "active"

    df["current_plan"] = df["current_plan"].fillna(df["starting_plan"])
    df["status"] = df["status"].fillna("active")
    return df


# ── Deciding what happens to an account today ────────────────────────────────

def decide_plan_change(account) -> str | None:
    """Returns 'upgrade', 'downgrade', 'cancel' or None."""
    archetype = account["archetype"]
    plan = account["current_plan"]
    limits = PLAN_LIMITS[plan]

    credit_ratio = account["credits_30d"] / limits["credits"]
    over_seats = account["seats"] > limits["seats"]

    # Pushing against the plan: either burning credits or out of seats.
    if credit_ratio >= HIGH_USAGE or over_seats:
        if UPGRADE_PATH[plan] and random.random() < UPGRADE_CHANCE[archetype]:
            return "upgrade"

    # Gone quiet while paying for it. Nobody keeps paying forever.
    if plan != "free" and credit_ratio < LOW_USAGE:
        roll = random.random()
        if roll < CANCEL_CHANCE[archetype]:
            return "cancel"
        # Some would rather step down than leave entirely.
        if roll < CANCEL_CHANCE[archetype] * 1.5 and DOWNGRADE_PATH[plan]:
            return "downgrade"

    return None


def apply_plan_change(account, change: str) -> str:
    """Make the change in Stripe. Returns the account's new plan."""
    subscription_id = account["stripe_subscription_id"]
    plan = account["current_plan"]

    if change == "cancel":
        stripe.Subscription.cancel(subscription_id)
        return plan  # plan stays, but status becomes canceled

    new_plan = UPGRADE_PATH[plan] if change == "upgrade" else DOWNGRADE_PATH[plan]

    # To change a subscription's price you replace its item, so we need
    # the current item's id first.
    subscription = stripe.Subscription.retrieve(subscription_id)
    item_id = subscription["items"]["data"][0]["id"]

    stripe.Subscription.modify(
        subscription_id,
        items=[{"id": item_id, "price": os.environ[PLAN_PRICE_ENV[new_plan]]}],
        # Bill the difference straight away, like a real upgrade would.
        proration_behavior="create_prorations",
    )
    return new_plan


# ── A day of usage for one account ───────────────────────────────────────────

def generate_day_events(account, day: datetime) -> list[dict]:
    behaviour = BEHAVIOUR[account["archetype"]]
    domain = account["domain"]

    signup = pd.to_datetime(account["signup_date"])
    if signup.tzinfo is None:
        signup = signup.replace(tzinfo=timezone.utc)

    day_index = (day - signup).days
    if day_index < 0:
        return []

    seats = int(account["seats"])
    users = [f"user{i + 1}@{domain}" for i in range(seats)]

    events: list[dict] = []
    counts = behaviour(day_index, seats)

    for event_type, count in counts.items():
        for _ in range(count):
            occurred_at = day + timedelta(
                hours=random.randint(7, 20),
                minutes=random.randint(0, 59),
                seconds=random.randint(0, 59),
            )
            events.append(
                make_event(event_type, domain, random.choice(users), occurred_at)
            )
            if event_type == "teammate_invited":
                users.append(f"user{len(users) + 1}@{domain}")

    return events


# ── New signups ──────────────────────────────────────────────────────────────

def create_new_signup(hubspot: HubSpot, used_names: set) -> dict:
    archetype = pick_archetype()
    spec = ARCHETYPES[archetype]

    name = make_company_name()
    while name in used_names:
        name = make_company_name()
    used_names.add(name)

    first_name = fake.first_name()
    last_name = fake.last_name()
    domain = make_domain(name)

    profile = {
        "company_name": name,
        "domain": domain,
        "employees": random.randint(*spec["employees"]),
        "archetype": archetype,
        # Everyone starts on Free. Upgrades have to be earned.
        "plan": "free",
        "first_name": first_name,
        "last_name": last_name,
        "email": f"{first_name.lower()}.{last_name.lower()}@{domain}",
        "signup_date": datetime.now(timezone.utc),
        "billing_linked": random.random() > 0.10,
        "has_duplicate_contact": random.random() < 0.05,
    }

    company_id = create_company(hubspot, profile)
    contact_id = create_contact(hubspot, profile, company_id, profile["email"])
    customer_id, subscription_id = create_stripe_customer(profile, company_id)

    return {
        "company_name": name,
        "domain": domain,
        "archetype": archetype,
        "employees": profile["employees"],
        "starting_plan": "free",
        "current_plan": "free",
        "status": "active",
        "signup_date": profile["signup_date"],
        "billing_linked": profile["billing_linked"],
        "hubspot_company_id": company_id,
        "hubspot_contact_id": contact_id,
        "hubspot_duplicate_contact_id": None,
        "stripe_customer_id": customer_id,
        "stripe_subscription_id": subscription_id,
        "seeded_at": datetime.now(timezone.utc),
    }


# ── Saving ───────────────────────────────────────────────────────────────────

def save_events(client: bigquery.Client, events: list[dict]) -> None:
    if not events:
        return
    df = pd.DataFrame(events)
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
    client.load_table_from_dataframe(
        df, f"{client.project}.{EVENTS_TABLE}", job_config=job_config
    ).result()


def save_accounts(client: bigquery.Client, df: pd.DataFrame) -> None:
    # Drop the calculated columns, they get recomputed from events each run.
    df = df.drop(columns=["credits_30d", "active_users", "seats", "last_seen_at"])
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
    client.load_table_from_dataframe(
        df, f"{client.project}.{SIM_TABLE}", job_config=job_config
    ).result()


# ── Main ─────────────────────────────────────────────────────────────────────

def run_day() -> None:
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    hubspot = HubSpot(access_token=os.environ["HUBSPOT_TOKEN"])
    bq = get_bigquery_client()

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    accounts = load_account_state(bq)

    print(f"Simulating {today.date()} for {len(accounts)} accounts...\n")

    events: list[dict] = []
    changes = {"upgrade": 0, "downgrade": 0, "cancel": 0}

    for index, account in accounts.iterrows():
        if account["status"] != "active":
            continue  # churned accounts stop using the product

        events.extend(generate_day_events(account, today))

        change = decide_plan_change(account)
        if not change:
            continue

        try:
            new_plan = apply_plan_change(account, change)
            changes[change] += 1

            if change == "cancel":
                accounts.at[index, "status"] = "canceled"
            else:
                accounts.at[index, "current_plan"] = new_plan

            print(f"  {change:<10} {account['company_name']:<22} "
                  f"{account['current_plan']} -> {new_plan}")
        except Exception as error:  # noqa: BLE001
            print(f"  FAILED {change} for {account['company_name']}: {error}")

    # New companies discover FlowForge.
    used_names = set(accounts["company_name"])
    new_rows = []
    for _ in range(random.randint(*NEW_SIGNUPS_PER_DAY)):
        try:
            new_rows.append(create_new_signup(hubspot, used_names))
        except Exception as error:  # noqa: BLE001
            print(f"  FAILED new signup: {error}")

    if new_rows:
        accounts = pd.concat([accounts, pd.DataFrame(new_rows)], ignore_index=True)

    save_events(bq, events)
    save_accounts(bq, accounts)

    print(f"\n{len(events):,} events, {changes['upgrade']} upgrades, "
          f"{changes['downgrade']} downgrades, {changes['cancel']} cancellations, "
          f"{len(new_rows)} new signups.")


if __name__ == "__main__":
    run_day()
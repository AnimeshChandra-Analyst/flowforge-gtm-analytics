"""
Usage event backfill for FlowForge.

Generates 30 days of product usage events for every seeded account, with
each account behaving according to its hidden archetype.

This is what makes the data worth analysing. Without it, every account
looks identical and no model could ever tell them apart.

Events land in flowforge_raw.raw_usage_events, which IS visible to the
data team (unlike the archetypes in flowforge_sim).

Run this once after seed_world.py. It replaces the table each time.
"""

import os
import random
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
from dotenv import load_dotenv
from google.cloud import bigquery
from google.oauth2 import service_account

load_dotenv()

# ── Settings ─────────────────────────────────────────────────────────────────

BACKFILL_DAYS = 30  # "our event tracking started 30 days ago"
RANDOM_SEED = 42

SIM_TABLE = "flowforge_sim.sim_accounts"
EVENTS_TABLE = "flowforge_raw.raw_usage_events"

# Planted mess: some events can't be attributed to an account, and some
# arrive late. Both are extremely common in real event pipelines.
MISSING_DOMAIN_RATE = 0.03
LATE_ARRIVAL_RATE = 0.05
MAX_LATE_DAYS = 2

# One prompt_run costs one AI credit. Everything else is free.
CREDIT_COST = {
    "session_started": 0,
    "prompt_run": 1,
    "project_created": 0,
    "teammate_invited": 0,
    "project_published": 0,
}

random.seed(RANDOM_SEED)


# ── How each archetype behaves on a given day ────────────────────────────────
#
# Each function returns how many of each event happen on one day.
# day_index is days since the account signed up.
# seats is how many users the account currently has.

def ghost_day(day_index: int, seats: int) -> dict:
    """Tries it once, then disappears. Half of all signups."""
    if day_index == 0:
        return {
            "session_started": 1,
            "prompt_run": random.randint(2, 5),
            "project_created": 1,
            "teammate_invited": 0,
            "project_published": 0,
        }
    # A tiny chance they wander back in, then nothing.
    if day_index <= 3 and random.random() < 0.15:
        return {"session_started": 1, "prompt_run": random.randint(1, 2),
                "project_created": 0, "teammate_invited": 0, "project_published": 0}
    if random.random() < 0.01:
        return {"session_started": 1, "prompt_run": 0, "project_created": 0,
                "teammate_invited": 0, "project_published": 0}
    return {}


def power_solo_day(day_index: int, seats: int) -> dict:
    """One person, uses it hard, almost every day. Never invites anyone."""
    if random.random() > 0.85:  # occasional day off
        return {}
    return {
        "session_started": random.randint(1, 3),
        "prompt_run": random.randint(8, 25),
        "project_created": 1 if random.random() < 0.3 else 0,
        "teammate_invited": 0,
        "project_published": 1 if random.random() < 0.4 else 0,
    }


def skeptic_exec_day(day_index: int, seats: int) -> dict:
    """Signed up to evaluate it. Pokes around once or twice a week."""
    if random.random() > 0.22:
        return {}
    return {
        "session_started": 1,
        "prompt_run": random.randint(1, 5),
        "project_created": 1 if random.random() < 0.15 else 0,
        # Might pull in one colleague early on to take a look.
        "teammate_invited": 1 if (day_index < 14 and random.random() < 0.05) else 0,
        "project_published": 0,
    }


def champion_day(day_index: int, seats: int) -> dict:
    """Starts small, then spreads through the company. This is who sales wants."""
    weeks = day_index / 7

    # Activity ramps up over the first couple of months, then plateaus.
    active_chance = min(0.55 + weeks * 0.06, 0.92)
    if random.random() > active_chance:
        return {}

    # More seats means more prompts, because more people are using it.
    base_prompts = random.randint(4, 12)
    prompts = int(base_prompts * (1 + seats * 0.5))

    # Invites get more likely as the team gets bolder, but slow down
    # once lots of people are already on board.
    invite_chance = 0.20 if seats < 8 else 0.02

    return {
        "session_started": random.randint(1, max(2, seats)),
        "prompt_run": prompts,
        "project_created": 1 if random.random() < 0.35 else 0,
        "teammate_invited": 1 if random.random() < invite_chance else 0,
        "project_published": 1 if random.random() < 0.3 else 0,
    }


BEHAVIOUR = {
    "ghost": ghost_day,
    "power_solo": power_solo_day,
    "skeptic_exec": skeptic_exec_day,
    "champion": champion_day,
}


# ── BigQuery ─────────────────────────────────────────────────────────────────

def get_bigquery_client() -> bigquery.Client:
    key_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    credentials = service_account.Credentials.from_service_account_file(key_path)
    return bigquery.Client(credentials=credentials, project=os.environ["BQ_PROJECT"])


def load_accounts(client: bigquery.Client) -> list[dict]:
    query = f"""
        SELECT company_name, domain, archetype, signup_date
        FROM `{client.project}.{SIM_TABLE}`
    """
    return client.query(query).to_dataframe().to_dict(orient="records")


# ── Generating events ────────────────────────────────────────────────────────

def make_event(
    event_type: str,
    domain: str,
    user_email: str,
    occurred_at: datetime,
) -> dict:
    # Most events are recorded the moment they happen. A few turn up late,
    # which is why incremental models need a lookback window.
    if random.random() < LATE_ARRIVAL_RATE:
        ingested_at = occurred_at + timedelta(
            days=random.randint(1, MAX_LATE_DAYS),
            minutes=random.randint(0, 600),
        )
    else:
        ingested_at = occurred_at + timedelta(seconds=random.randint(1, 30))

    # And a few lose their account entirely, so they can't be attributed.
    recorded_domain = None if random.random() < MISSING_DOMAIN_RATE else domain

    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "account_domain": recorded_domain,
        "user_email": user_email,
        "credits_used": CREDIT_COST[event_type],
        "occurred_at": occurred_at,
        "ingested_at": ingested_at,
    }


def generate_for_account(account: dict, start_date: datetime, end_date: datetime) -> list[dict]:
    behaviour = BEHAVIOUR[account["archetype"]]
    domain = account["domain"]

    signup = account["signup_date"]
    if isinstance(signup, str):
        signup = pd.to_datetime(signup)
    signup = signup.replace(tzinfo=timezone.utc) if signup.tzinfo is None else signup

    # Accounts start with one user. Every teammate_invited adds another.
    users = [f"user1@{domain}"]
    events: list[dict] = []

    day = start_date
    while day <= end_date:
        # Nothing happens before they signed up.
        if day < signup.replace(hour=0, minute=0, second=0, microsecond=0):
            day += timedelta(days=1)
            continue

        day_index = (day - signup).days
        counts = behaviour(day_index, len(users))

        for event_type, count in counts.items():
            for _ in range(count):
                # Spread events through the working day.
                occurred_at = day + timedelta(
                    hours=random.randint(7, 20),
                    minutes=random.randint(0, 59),
                    seconds=random.randint(0, 59),
                )
                user_email = random.choice(users)
                events.append(make_event(event_type, domain, user_email, occurred_at))

                if event_type == "teammate_invited":
                    users.append(f"user{len(users) + 1}@{domain}")

        day += timedelta(days=1)

    return events


def run_backfill() -> None:
    client = get_bigquery_client()
    accounts = load_accounts(client)
    print(f"Generating {BACKFILL_DAYS} days of usage for {len(accounts)} accounts...\n")

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = today - timedelta(days=BACKFILL_DAYS)
    end_date = today - timedelta(days=1)  # up to yesterday

    all_events: list[dict] = []
    for account in accounts:
        events = generate_for_account(account, start_date, end_date)
        all_events.extend(events)

    df = pd.DataFrame(all_events)
    df = df.sort_values("occurred_at").reset_index(drop=True)

    table_id = f"{client.project}.{EVENTS_TABLE}"
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_TRUNCATE",
        autodetect=True,
    )
    client.load_table_from_dataframe(df, table_id, job_config=job_config).result()

    print(f"Loaded {len(df):,} events into {table_id}")
    print("\nEvents by type:")
    print(df["event_type"].value_counts().to_string())
    print(f"\nEvents with no account: {df['account_domain'].isna().sum():,}")


if __name__ == "__main__":
    run_backfill()
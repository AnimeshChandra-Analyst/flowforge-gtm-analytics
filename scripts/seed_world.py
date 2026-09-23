"""
Seed script for FlowForge.

Creates the starting world: a set of companies that already exist as
FlowForge customers on day zero.

For each company it:
  1. picks a hidden archetype (Ghost, Power Solo, Skeptic Exec, Champion)
  2. picks an employee count and starting plan that fit that archetype
  3. creates the company + a contact in HubSpot
  4. creates the same company as a customer in Stripe, on that plan
  5. records the archetype in BigQuery (the "answer key")

IMPORTANT: run this ONCE. If it fails halfway, clean up before rerunning
(see cleanup_seed.py) or you'll end up with duplicates.

Start with N_COMPANIES = 5 to check it works, then bump it to 200.
"""

import os
import random
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import stripe
from dotenv import load_dotenv
from faker import Faker
from google.cloud import bigquery
from google.oauth2 import service_account
from hubspot import HubSpot
from hubspot.crm.companies import SimplePublicObjectInputForCreate as CompanyInput
from hubspot.crm.contacts import SimplePublicObjectInputForCreate as ContactInput

load_dotenv()

# ── Settings ─────────────────────────────────────────────────────────────────

N_COMPANIES = 200  # use 5 while testing. Max is 420 (the name combinations).

# Fixing the random seed means you get the same "random" results every run.
# Useful while testing, so a rerun is predictable.
RANDOM_SEED = 42

# How far back signups are spread. HubSpot and Stripe will stamp everything
# with today's date, so we keep our own signup_date for usage backfill later.
SIGNUP_WINDOW_DAYS = 90

# Planted data problems (see the project README)
DUPLICATE_CONTACT_RATE = 0.05   # same person, slightly different email
UNLINKED_BILLING_RATE = 0.10    # Stripe customer with no link back to HubSpot

# HubSpot allows 100 calls per 10 seconds. This keeps us well under.
SLEEP_BETWEEN_COMPANIES = 0.4

# The answer key lives in its own dataset, NOT in flowforge_raw.
# flowforge_raw is what "the data team" is allowed to see. This isn't.
SIM_DATASET = "flowforge_sim"
SIM_TABLE = "sim_accounts"

# Each archetype's behaviour. weight = share of signups.
ARCHETYPES = {
    "ghost": {
        "weight": 50,
        "employees": (1, 10),
        "plans": {"free": 1.0},
    },
    "power_solo": {
        "weight": 20,
        "employees": (1, 1),
        "plans": {"free": 0.25, "pro": 0.75},
    },
    "skeptic_exec": {
        "weight": 15,
        "employees": (100, 500),
        "plans": {"free": 0.60, "pro": 0.40},
    },
    "champion": {
        "weight": 15,
        "employees": (50, 300),
        "plans": {"free": 0.50, "pro": 0.35, "team": 0.15},
    },
}

# Enterprise is deliberately missing here: nobody starts on Enterprise.
# You only get there by winning a deal, which the daily script handles.
PLAN_PRICE_ENV = {
    "free": "PRICE_FREE",
    "pro": "PRICE_PRO",
    "team": "PRICE_TEAM",
    "enterprise": "PRICE_ENTERPRISE",
}

# Faker's built-in company names look like law firms ("Guzman, Hoffman and
# Baldwin"), which doesn't suit a customer base of AI app builders. So we
# build our own from two word lists instead.
# 28 x 15 = 420 possible names, comfortably more than the 200 we need.
NAME_PARTS_A = [
    "Nord", "Volt", "Pixel", "Hyper", "Lumen", "Flux", "Orbit", "Basil",
    "Corner", "Fern", "Tide", "Atlas", "Kite", "Maple", "Quartz", "Drift",
    "Kora", "Silo", "Azent", "Elvo", "Ember", "Cobalt", "Harbor", "Vantage",
    "Juniper", "Solna", "Aster", "Rune",
]
NAME_PARTS_B = [
    "Labs", "Works", "Studio", "Digital", "Group", "Collective", "Systems",
    "AB", "Technologies", "Partners", "Ventures", "Agency", "Corp", "Co",
    "AI",
]

fake = Faker()
Faker.seed(RANDOM_SEED)
random.seed(RANDOM_SEED)


def make_company_name() -> str:
    """e.g. 'NordLabs', 'PixelStudio', 'TideVentures'."""
    return f"{random.choice(NAME_PARTS_A)}{random.choice(NAME_PARTS_B)}"


# ── Clients ──────────────────────────────────────────────────────────────────

def get_hubspot_client() -> HubSpot:
    return HubSpot(access_token=os.environ["HUBSPOT_TOKEN"])


def get_bigquery_client() -> bigquery.Client:
    key_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    credentials = service_account.Credentials.from_service_account_file(key_path)
    return bigquery.Client(credentials=credentials, project=os.environ["BQ_PROJECT"])


# ── Building one fake company ────────────────────────────────────────────────

def pick_archetype() -> str:
    names = list(ARCHETYPES.keys())
    weights = [ARCHETYPES[n]["weight"] for n in names]
    return random.choices(names, weights=weights, k=1)[0]


def pick_plan(archetype: str) -> str:
    plans = ARCHETYPES[archetype]["plans"]
    return random.choices(list(plans.keys()), weights=list(plans.values()), k=1)[0]


def make_domain(company_name: str) -> str:
    """Turn 'Hansen, Miller and Co' into 'hansenmillerandco.com'."""
    cleaned = "".join(c for c in company_name.lower() if c.isalnum())
    return f"{cleaned[:24]}.com"


def build_profile(used_names: set) -> dict:
    """Invent one company, including the bits only the simulator knows."""
    archetype = pick_archetype()
    spec = ARCHETYPES[archetype]

    # Keep company names unique so we don't confuse ourselves later.
    name = make_company_name()
    while name in used_names:
        name = make_company_name()
    used_names.add(name)

    first_name = fake.first_name()
    last_name = fake.last_name()
    domain = make_domain(name)

    signup_date = datetime.now(timezone.utc) - timedelta(
        days=random.randint(1, SIGNUP_WINDOW_DAYS)
    )

    return {
        "company_name": name,
        "domain": domain,
        "employees": random.randint(*spec["employees"]),
        "archetype": archetype,
        "plan": pick_plan(archetype),
        "first_name": first_name,
        "last_name": last_name,
        "email": f"{first_name.lower()}.{last_name.lower()}@{domain}",
        "signup_date": signup_date,
        # Planted mess: ~10% of customers pay with a personal address and
        # have no link back to the CRM, so we have to match them on domain
        # or name later instead of following an id.
        "billing_linked": random.random() > UNLINKED_BILLING_RATE,
        # Planted mess: ~5% of companies have the same person in the CRM
        # twice under slightly different emails.
        "has_duplicate_contact": random.random() < DUPLICATE_CONTACT_RATE,
    }


# ── HubSpot ──────────────────────────────────────────────────────────────────

def create_company(client: HubSpot, profile: dict) -> str:
    company = client.crm.companies.basic_api.create(
        simple_public_object_input_for_create=CompanyInput(
            properties={
                "name": profile["company_name"],
                "domain": profile["domain"],
                "numberofemployees": str(profile["employees"]),
            }
        )
    )
    return company.id


def create_contact(client: HubSpot, profile: dict, company_id: str, email: str) -> str:
    """Create a contact and link it to its company."""
    properties = {
        "email": email,
        "firstname": profile["first_name"],
        "lastname": profile["last_name"],
        "company": profile["company_name"],
    }

    contact = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=ContactInput(properties=properties)
    )

    # Link contact -> company. If HubSpot's association API gives us trouble
    # we carry on anyway: our dbt models can still match them on email domain,
    # which we need to do regardless because of the planted mess.
    try:
        client.crm.associations.v4.basic_api.create_default(
            from_object_type="contacts",
            from_object_id=contact.id,
            to_object_type="companies",
            to_object_id=company_id,
        )
    except Exception as error:  # noqa: BLE001
        print(f"    (could not associate contact to company: {error})")

    return contact.id


def duplicate_email(email: str) -> str:
    """john.smith@acme.com -> johnsmith@acme.com

    HubSpot blocks two contacts with the exact same email, so real-world
    duplicates always look slightly different. This mimics that.
    """
    local, domain = email.split("@")
    return f"{local.replace('.', '')}@{domain}"


# ── Stripe ───────────────────────────────────────────────────────────────────

def create_stripe_customer(profile: dict, hubspot_company_id: str) -> tuple[str, str]:
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

    # Unlinked customers pay with a personal address instead of a work one.
    billing_email = (
        profile["email"]
        if profile["billing_linked"]
        else f"{profile['first_name'].lower()}{random.randint(1, 99)}@gmail.com"
    )

    # metadata is Stripe's free-text notes field. Storing the HubSpot id here
    # is the thread that ties the two systems together.
    metadata = (
        {"hubspot_company_id": hubspot_company_id}
        if profile["billing_linked"]
        else {}
    )

    customer = stripe.Customer.create(
        name=profile["company_name"],  # same name as HubSpot, on purpose
        email=billing_email,
        payment_method="pm_card_visa",  # Stripe's fake test card
        invoice_settings={"default_payment_method": "pm_card_visa"},
        metadata=metadata,
    )

    subscription = stripe.Subscription.create(
        customer=customer.id,
        items=[{"price": os.environ[PLAN_PRICE_ENV[profile["plan"]]]}],
    )

    return customer.id, subscription.id


# ── Saving the answer key ────────────────────────────────────────────────────

def save_answer_key(rows: list[dict]) -> None:
    client = get_bigquery_client()

    # Create the dataset if it isn't there yet.
    dataset_id = f"{client.project}.{SIM_DATASET}"
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = "EU"
    client.create_dataset(dataset, exists_ok=True)

    df = pd.DataFrame(rows)
    table_id = f"{dataset_id}.{SIM_TABLE}"

    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_TRUNCATE",
        autodetect=True,
    )
    client.load_table_from_dataframe(df, table_id, job_config=job_config).result()
    print(f"\nAnswer key saved: {len(df)} rows in {table_id}")


# ── Main ─────────────────────────────────────────────────────────────────────

def run_seed() -> None:
    hubspot = get_hubspot_client()
    used_names: set = set()
    rows: list[dict] = []
    failures = 0

    print(f"Seeding {N_COMPANIES} companies...\n")

    for i in range(1, N_COMPANIES + 1):
        profile = build_profile(used_names)

        try:
            company_id = create_company(hubspot, profile)
            contact_id = create_contact(
                hubspot, profile, company_id, profile["email"]
            )

            duplicate_contact_id = None
            if profile["has_duplicate_contact"]:
                duplicate_contact_id = create_contact(
                    hubspot, profile, company_id, duplicate_email(profile["email"])
                )

            customer_id, subscription_id = create_stripe_customer(profile, company_id)

            rows.append(
                {
                    "company_name": profile["company_name"],
                    "domain": profile["domain"],
                    "archetype": profile["archetype"],
                    "employees": profile["employees"],
                    "starting_plan": profile["plan"],
                    "signup_date": profile["signup_date"],
                    "billing_linked": profile["billing_linked"],
                    "hubspot_company_id": company_id,
                    "hubspot_contact_id": contact_id,
                    "hubspot_duplicate_contact_id": duplicate_contact_id,
                    "stripe_customer_id": customer_id,
                    "stripe_subscription_id": subscription_id,
                    "seeded_at": datetime.now(timezone.utc),
                }
            )

            print(
                f"  {i:>3}/{N_COMPANIES}  {profile['company_name'][:32]:<34}"
                f"{profile['archetype']:<14}{profile['plan']}"
            )

        except Exception as error:  # noqa: BLE001
            failures += 1
            print(f"  {i:>3}/{N_COMPANIES}  FAILED: {error}")

        time.sleep(SLEEP_BETWEEN_COMPANIES)

    if rows:
        save_answer_key(rows)

    print(f"\nDone. {len(rows)} created, {failures} failed.")


if __name__ == "__main__":
    run_seed()
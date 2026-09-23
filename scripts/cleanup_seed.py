"""
Cleanup script for FlowForge.

Undoes seed_world.py. Reads every id out of the answer key table and
deletes those records from HubSpot and Stripe, then drops the table.

Use it when you want to start the world over from scratch.

Note on what "delete" means:
  HubSpot archives records (they go to the recycle bin, recoverable for
  a while), Stripe deletes customers properly and cancels their
  subscriptions automatically.
"""

import os
import time

from dotenv import load_dotenv
from google.cloud import bigquery
from google.oauth2 import service_account
from hubspot import HubSpot
import stripe

load_dotenv()

SIM_DATASET = "flowforge_sim"
SIM_TABLE = "sim_accounts"
SLEEP_BETWEEN_ROWS = 0.3


def get_hubspot_client() -> HubSpot:
    return HubSpot(access_token=os.environ["HUBSPOT_TOKEN"])


def get_bigquery_client() -> bigquery.Client:
    key_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    credentials = service_account.Credentials.from_service_account_file(key_path)
    return bigquery.Client(credentials=credentials, project=os.environ["BQ_PROJECT"])


def load_seeded_rows(client: bigquery.Client) -> list[dict]:
    table_id = f"{client.project}.{SIM_DATASET}.{SIM_TABLE}"
    try:
        rows = client.query(f"SELECT * FROM `{table_id}`").to_dataframe()
    except Exception as error:  # noqa: BLE001
        print(f"Could not read {table_id}: {error}")
        return []
    return rows.to_dict(orient="records")


def delete_from_hubspot(client: HubSpot, row: dict) -> None:
    # Contacts first, then the company. Order doesn't strictly matter here,
    # but deleting children before parents is a good habit.
    for key in ("hubspot_contact_id", "hubspot_duplicate_contact_id"):
        contact_id = row.get(key)
        if contact_id and str(contact_id) != "None":
            try:
                client.crm.contacts.basic_api.archive(contact_id=str(contact_id))
            except Exception as error:  # noqa: BLE001
                print(f"    contact {contact_id}: {error}")

    company_id = row.get("hubspot_company_id")
    if company_id:
        try:
            client.crm.companies.basic_api.archive(company_id=str(company_id))
        except Exception as error:  # noqa: BLE001
            print(f"    company {company_id}: {error}")


def delete_from_stripe(row: dict) -> None:
    customer_id = row.get("stripe_customer_id")
    if not customer_id:
        return
    try:
        # Deleting a customer cancels their subscriptions too, so we don't
        # need to cancel the subscription separately.
        stripe.Customer.delete(str(customer_id))
    except Exception as error:  # noqa: BLE001
        print(f"    stripe customer {customer_id}: {error}")


def drop_answer_key(client: bigquery.Client) -> None:
    table_id = f"{client.project}.{SIM_DATASET}.{SIM_TABLE}"
    client.delete_table(table_id, not_found_ok=True)
    print(f"Dropped {table_id}")


def run_cleanup() -> None:
    bq = get_bigquery_client()
    rows = load_seeded_rows(bq)

    if not rows:
        print("Nothing to clean up.")
        return

    print(f"This will delete {len(rows)} companies from HubSpot and Stripe.")
    answer = input("Type DELETE to confirm: ")
    if answer.strip() != "DELETE":
        print("Cancelled.")
        return

    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    hubspot = get_hubspot_client()

    for i, row in enumerate(rows, start=1):
        name = row.get("company_name", "?")
        print(f"  {i:>3}/{len(rows)}  {name}")
        delete_from_hubspot(hubspot, row)
        delete_from_stripe(row)
        time.sleep(SLEEP_BETWEEN_ROWS)

    drop_answer_key(bq)
    print("\nWorld reset. You can run seed_world.py again.")


if __name__ == "__main__":
    run_cleanup()
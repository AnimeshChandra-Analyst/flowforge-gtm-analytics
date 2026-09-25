"""
Extract script for FlowForge.

This is the data team's first job: pull everything out of HubSpot and Stripe
and land it in BigQuery, untouched.

  HubSpot: companies, contacts, deals
  Stripe:  customers, subscriptions, invoices

Design choices (see README for the reasoning):
  - Full refresh: every run pulls everything and replaces the table.
  - Raw JSON: each record is stored whole as a JSON string. All the
    picking apart of fields happens later in dbt, not here.
"""

import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import stripe
from dotenv import load_dotenv
from google.cloud import bigquery
from google.oauth2 import service_account
from hubspot import HubSpot

load_dotenv()

RAW_DATASET = os.environ.get("BQ_RAW_DATASET", "flowforge_raw")
PAGE_SIZE = 100  # the most either API will give us in one go

# ── What to pull from HubSpot ────────────────────────────────────────────────
#
# Gotcha: HubSpot only returns a handful of default fields unless you ask
# for the ones you want by name. Forget a field here and it silently won't
# be in your data. That's why these lists are explicit.

HUBSPOT_OBJECTS = {
    "companies": {
        "properties": [
            "name", "domain", "numberofemployees",
            "createdate", "hs_lastmodifieddate",
        ],
        "associations": [],
    },
    "contacts": {
        "properties": [
            "email", "firstname", "lastname", "company",
            "createdate", "lastmodifieddate",
        ],
        # Ask HubSpot to tell us which company each contact belongs to.
        "associations": ["companies"],
    },
    "deals": {
        "properties": [
            "dealname", "dealstage", "pipeline", "amount",
            "closedate", "createdate", "hs_lastmodifieddate",
        ],
        "associations": ["companies"],
    },
}

# ── What to pull from Stripe ─────────────────────────────────────────────────
#
# Gotcha: by default Stripe only lists ACTIVE subscriptions. Cancelled ones
# are hidden unless you ask for status="all". Miss this and churn would be
# invisible in your data, which would wreck every revenue metric.

STRIPE_OBJECTS = {
    "customers": {"resource": stripe.Customer, "params": {}},
    "subscriptions": {"resource": stripe.Subscription, "params": {"status": "all"}},
    "invoices": {"resource": stripe.Invoice, "params": {}},
}


# ── Clients ──────────────────────────────────────────────────────────────────

def get_bigquery_client() -> bigquery.Client:
    key_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    credentials = service_account.Credentials.from_service_account_file(key_path)
    return bigquery.Client(credentials=credentials, project=os.environ["BQ_PROJECT"])


# ── HubSpot: paging through results ──────────────────────────────────────────
#
# HubSpot pages with a cursor. Each response includes a token called
# "after" if there's more to fetch. You send that token back on the next
# request to say "carry on from here". No token means you've reached the end.

def fetch_hubspot(client: HubSpot, object_name: str) -> list[dict]:
    config = HUBSPOT_OBJECTS[object_name]
    api = getattr(client.crm, object_name).basic_api

    records: list[dict] = []
    after = None
    page_number = 0

    while True:
        page_number += 1
        page = api.get_page(
            limit=PAGE_SIZE,
            after=after,
            properties=config["properties"],
            associations=config["associations"] or None,
            archived=False,
        )

        for item in page.results:
            records.append(
                {"id": str(item.id), "raw_json": json.dumps(item.to_dict(), default=str)}
            )

        # Is there another page? If so, grab the cursor and go again.
        if page.paging and page.paging.next:
            after = page.paging.next.after
            time.sleep(0.2)  # be polite to the API
        else:
            break

    print(f"  hubspot {object_name:<14} {len(records):>5} records  ({page_number} pages)")
    return records


# ── Stripe: paging through results ───────────────────────────────────────────
#
# Stripe pages differently. Each response has has_more (true/false). To get
# the next page you pass starting_after = the id of the LAST record you got,
# meaning "give me the ones after this one".

def stripe_to_json(obj) -> str:
    if hasattr(obj, "to_dict"):
        return json.dumps(obj.to_dict(), default=str)
    return str(obj)  # StripeObject prints itself as JSON


def fetch_stripe(object_name: str) -> list[dict]:
    config = STRIPE_OBJECTS[object_name]
    resource = config["resource"]

    records: list[dict] = []
    starting_after = None
    page_number = 0

    while True:
        page_number += 1
        params = {"limit": PAGE_SIZE, **config["params"]}
        if starting_after:
            params["starting_after"] = starting_after

        page = resource.list(**params)

        for item in page.data:
            records.append({"id": item.id, "raw_json": stripe_to_json(item)})

        if page.has_more and page.data:
            starting_after = page.data[-1].id
            time.sleep(0.2)
        else:
            break

    print(f"  stripe  {object_name:<14} {len(records):>5} records  ({page_number} pages)")
    return records


# ── Loading into BigQuery ────────────────────────────────────────────────────
#
# Every raw table has the same three columns. Keeping the shape identical
# means this one function can load all six tables, and an empty table
# (like deals, which has nothing in it yet) still gets created properly.

RAW_SCHEMA = [
    bigquery.SchemaField("id", "STRING"),
    bigquery.SchemaField("raw_json", "STRING"),
    bigquery.SchemaField("extracted_at", "TIMESTAMP"),
]


def load_table(client: bigquery.Client, table_name: str, records: list[dict]) -> None:
    table_id = f"{client.project}.{RAW_DATASET}.{table_name}"
    extracted_at = datetime.now(timezone.utc)

    if not records:
        # Nothing to load, but downstream models still expect the table
        # to exist, so create it empty with the right columns.
        client.query(
            f"CREATE OR REPLACE TABLE `{table_id}` "
            f"(id STRING, raw_json STRING, extracted_at TIMESTAMP)"
        ).result()
        return

    df = pd.DataFrame(records)
    df["extracted_at"] = extracted_at

    job_config = bigquery.LoadJobConfig(
        schema=RAW_SCHEMA,
        write_disposition="WRITE_TRUNCATE",  # full refresh: replace the table
    )
    client.load_table_from_dataframe(df, table_id, job_config=job_config).result()


# ── Main ─────────────────────────────────────────────────────────────────────

def run_extract() -> None:
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    hubspot = HubSpot(access_token=os.environ["HUBSPOT_TOKEN"])
    bq = get_bigquery_client()

    print("Extracting...\n")

    for object_name in HUBSPOT_OBJECTS:
        records = fetch_hubspot(hubspot, object_name)
        load_table(bq, f"raw_hubspot_{object_name}", records)

    for object_name in STRIPE_OBJECTS:
        records = fetch_stripe(object_name)
        load_table(bq, f"raw_stripe_{object_name}", records)

    print(f"\nDone. Tables are in {bq.project}.{RAW_DATASET}")


if __name__ == "__main__":
    run_extract()
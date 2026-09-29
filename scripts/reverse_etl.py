"""
Reverse ETL for FlowForge.

Takes what the marts worked out and pushes it back into HubSpot, so sales
and customer success see it where they already work instead of having to
open a dashboard.

Two jobs:

  1. Write account data onto every company record: health status, usage,
     how close they are to their plan limits, revenue, segment.
  2. Create deals for accounts that became PQLs, without creating a
     second deal for accounts that already have one.

Run after dbt, since it reads the marts.
"""

import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from google.cloud import bigquery
from google.oauth2 import service_account
from hubspot import HubSpot
from hubspot.crm.companies import BatchInputSimplePublicObjectBatchInput
from hubspot.crm.deals import SimplePublicObjectInputForCreate as DealInput
from hubspot.crm.properties import PropertyCreate

load_dotenv()

MARTS_DATASET = "marts"
HUBSPOT_PQL_STAGE = "appointmentscheduled"  # internal id of "Qualified PQL"
BATCH_SIZE = 100  # HubSpot's limit for batch updates
SLEEP_BETWEEN_CALLS = 0.3

# ── The custom fields we write onto companies ────────────────────────────────
#
# HubSpot won't accept a value for a property that doesn't exist yet, so the
# script creates any that are missing on first run. Names are prefixed with
# flowforge_ so it's obvious which fields come from the data pipeline rather
# than from someone typing into the CRM.

COMPANY_PROPERTIES = [
    # health and engagement
    ("flowforge_health_status", "Health Status", "string"),
    ("flowforge_account_segment", "Account Segment", "string"),
    ("flowforge_days_since_active", "Days Since Last Active", "number"),
    ("flowforge_active_users_14d", "Active Users (14d)", "number"),
    # plan and limits
    ("flowforge_plan", "Current Plan", "string"),
    ("flowforge_credit_usage_pct", "Credit Usage %", "number"),
    ("flowforge_seat_usage_pct", "Seat Usage %", "number"),
    ("flowforge_credits_used_14d", "Credits Used (14d)", "number"),
    # money
    ("flowforge_current_mrr", "Current MRR (USD)", "number"),
    ("flowforge_lifetime_revenue", "Lifetime Revenue (USD)", "number"),
    ("flowforge_tenure_days", "Customer Tenure (days)", "number"),
    # sales signals
    ("flowforge_is_pql", "Is PQL", "string"),
    ("flowforge_pql_signals", "PQL Signal Count", "number"),
    # so anyone can see how fresh this is
    ("flowforge_synced_at", "Data Last Synced", "string"),
]

# Which mart column feeds which HubSpot property.
PROPERTY_MAP = {
    "flowforge_health_status": "health_status",
    "flowforge_account_segment": "account_segment",
    "flowforge_days_since_active": "days_since_last_active",
    "flowforge_active_users_14d": "active_users_14d",
    "flowforge_plan": "plan_name",
    "flowforge_credit_usage_pct": "credit_usage_pct",
    "flowforge_seat_usage_pct": "seat_usage_pct",
    "flowforge_credits_used_14d": "credits_used_14d",
    "flowforge_current_mrr": "current_mrr_usd",
    "flowforge_lifetime_revenue": "lifetime_revenue_usd",
    "flowforge_tenure_days": "tenure_days",
    "flowforge_pql_signals": "pql_signal_count",
}


# ── Clients ──────────────────────────────────────────────────────────────────

def get_bigquery_client() -> bigquery.Client:
    key_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    credentials = service_account.Credentials.from_service_account_file(key_path)
    return bigquery.Client(credentials=credentials, project=os.environ["BQ_PROJECT"])


def get_hubspot_client() -> HubSpot:
    return HubSpot(access_token=os.environ["HUBSPOT_TOKEN"])


# ── Making sure the fields exist ─────────────────────────────────────────────

def ensure_company_properties(client: HubSpot) -> None:
    """Create any flowforge_ fields that aren't in HubSpot yet."""
    existing = {
        p.name
        for p in client.crm.properties.core_api.get_all(object_type="companies").results
    }

    created = 0
    for name, label, field_type in COMPANY_PROPERTIES:
        if name in existing:
            continue
        try:
            client.crm.properties.core_api.create(
                object_type="companies",
                property_create=PropertyCreate(
                    name=name,
                    label=label,
                    group_name="companyinformation",
                    type=field_type,
                    field_type="text" if field_type == "string" else "number",
                ),
            )
            created += 1
            time.sleep(SLEEP_BETWEEN_CALLS)
        except Exception as error:  # noqa: BLE001
            print(f"  could not create {name}: {error}")

    print(f"Company properties: {len(existing & {p[0] for p in COMPANY_PROPERTIES})} "
          f"already there, {created} created")


# ── Reading the marts ────────────────────────────────────────────────────────

def load_account_overview(bq: bigquery.Client) -> list[dict]:
    query = f"""
        select *
        from `{bq.project}.{MARTS_DATASET}.mart_account_overview`
        where hubspot_company_id is not null
    """
    return bq.query(query).to_dataframe().to_dict(orient="records")


def load_pqls(bq: bigquery.Client) -> list[dict]:
    query = f"""
        select *
        from `{bq.project}.{MARTS_DATASET}.mart_product_qualified_leads`
    """
    return bq.query(query).to_dataframe().to_dict(orient="records")


# ── Writing company properties ───────────────────────────────────────────────

def clean_value(value) -> str:
    """HubSpot wants strings, and empty rather than the text 'None'."""
    if value is None:
        return ""
    try:
        import pandas as pd

        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def sync_company_properties(client: HubSpot, accounts: list[dict]) -> None:
    synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    updates = []
    for account in accounts:
        properties = {
            hubspot_field: clean_value(account.get(mart_column))
            for hubspot_field, mart_column in PROPERTY_MAP.items()
        }
        # Booleans read better as Yes/No in a CRM than as true/false.
        properties["flowforge_is_pql"] = "Yes" if account.get("is_pql") else "No"
        properties["flowforge_synced_at"] = synced_at

        updates.append(
            {"id": str(account["hubspot_company_id"]), "properties": properties}
        )

    # HubSpot takes 100 records per batch call, so 230 accounts is 3 calls
    # instead of 230. Much faster and much kinder to the rate limit.
    updated = 0
    for start in range(0, len(updates), BATCH_SIZE):
        batch = updates[start:start + BATCH_SIZE]
        try:
            client.crm.companies.batch_api.update(
                batch_input_simple_public_object_batch_input=(
                    BatchInputSimplePublicObjectBatchInput(inputs=batch)
                )
            )
            updated += len(batch)
            time.sleep(SLEEP_BETWEEN_CALLS)
        except Exception as error:  # noqa: BLE001
            print(f"  batch starting at {start} failed: {error}")

    print(f"Company properties: {updated} companies updated")


# ── Creating deals for new PQLs ──────────────────────────────────────────────

def existing_deal_company_ids(client: HubSpot) -> set:
    """Which companies already have a FlowForge-created deal.

    Without this the script would create a fresh deal every single day for
    the same accounts, since a PQL stays a PQL until something changes.
    """
    company_ids = set()
    after = None

    while True:
        page = client.crm.deals.basic_api.get_page(
            limit=100,
            after=after,
            properties=["dealname", "dealstage", "flowforge_company_id"],
            archived=False,
        )
        for deal in page.results:
            company_id = (deal.properties or {}).get("flowforge_company_id")
            if company_id:
                company_ids.add(str(company_id))

        if page.paging and page.paging.next:
            after = page.paging.next.after
            time.sleep(SLEEP_BETWEEN_CALLS)
        else:
            break

    return company_ids


def ensure_deal_properties(client: HubSpot) -> None:
    """A field on deals holding the company id.

    HubSpot associations come back empty for this account, so the company
    id is written onto the deal directly. That's what lets us tell whether
    a deal already exists for an account.
    """
    existing = {
        p.name
        for p in client.crm.properties.core_api.get_all(object_type="deals").results
    }

    wanted = [
        ("flowforge_company_id", "FlowForge Company ID", "string"),
        ("flowforge_pql_signals", "PQL Signal Count", "number"),
        ("flowforge_credit_usage_pct", "Credit Usage % at Qualification", "number"),
        ("flowforge_active_users", "Active Users at Qualification", "number"),
    ]

    for name, label, field_type in wanted:
        if name in existing:
            continue
        try:
            client.crm.properties.core_api.create(
                object_type="deals",
                property_create=PropertyCreate(
                    name=name,
                    label=label,
                    group_name="dealinformation",
                    type=field_type,
                    field_type="text" if field_type == "string" else "number",
                ),
            )
            time.sleep(SLEEP_BETWEEN_CALLS)
        except Exception as error:  # noqa: BLE001
            print(f"  could not create deal property {name}: {error}")


def estimate_deal_value(account: dict) -> float:
    """A rough Enterprise annual value, so the pipeline has a number in it.

    Based on employee count, since that's the best proxy for how many
    seats they'd eventually need.
    """
    employees = account.get("total_employees") or 0
    if employees >= 250:
        return 24000.0
    if employees >= 100:
        return 18000.0
    return 12000.0


def create_pql_deals(client: HubSpot, pqls: list[dict], already_have: set) -> None:
    created = 0
    skipped = 0

    for account in pqls:
        company_id = str(account["hubspot_company_id"])

        if company_id in already_have:
            skipped += 1
            continue

        properties = {
            "dealname": f"{account['company_name']} - Enterprise (PQL)",
            "dealstage": HUBSPOT_PQL_STAGE,
            "amount": str(estimate_deal_value(account)),
            "flowforge_company_id": company_id,
            "flowforge_pql_signals": str(account.get("signal_count", "")),
            "flowforge_credit_usage_pct": clean_value(account.get("credit_usage_pct")),
            "flowforge_active_users": clean_value(account.get("active_users")),
        }

        try:
            deal = client.crm.deals.basic_api.create(
                simple_public_object_input_for_create=DealInput(properties=properties)
            )

            # Try to associate the deal to its company as well. The id is
            # already on the deal as a property, so this is a bonus rather
            # than something the pipeline depends on.
            try:
                client.crm.associations.v4.basic_api.create_default(
                    from_object_type="deals",
                    from_object_id=deal.id,
                    to_object_type="companies",
                    to_object_id=company_id,
                )
            except Exception:  # noqa: BLE001
                pass

            created += 1
            print(f"  new deal: {account['company_name']} "
                  f"({account.get('signal_count')} signals)")
            time.sleep(SLEEP_BETWEEN_CALLS)

        except Exception as error:  # noqa: BLE001
            print(f"  failed for {account['company_name']}: {error}")

    print(f"Deals: {created} created, {skipped} already existed")


# ── Main ─────────────────────────────────────────────────────────────────────

def run_reverse_etl() -> None:
    bq = get_bigquery_client()
    hubspot = get_hubspot_client()

    print("Reverse ETL: pushing marts back into HubSpot\n")

    ensure_company_properties(hubspot)
    ensure_deal_properties(hubspot)

    accounts = load_account_overview(bq)
    sync_company_properties(hubspot, accounts)

    pqls = load_pqls(bq)
    already_have = existing_deal_company_ids(hubspot)
    create_pql_deals(hubspot, pqls, already_have)

    print("\nDone.")


if __name__ == "__main__":
    run_reverse_etl()
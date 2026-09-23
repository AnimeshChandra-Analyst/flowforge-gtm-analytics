"""
Trial script for FlowForge.

Creates ONE fake company in HubSpot and ONE fake customer in Stripe,
just to prove our keys, permissions and libraries all work.

"""

import os

import stripe
from dotenv import load_dotenv
from faker import Faker
from hubspot import HubSpot
from hubspot.crm.companies import SimplePublicObjectInputForCreate

# Reads the .env file and makes those values available to this script.
load_dotenv()

# faker invents realistic fake names, emails, companies etc.
fake = Faker()


def create_hubspot_company() -> str:
    """Create one company in HubSpot and return its id."""
    client = HubSpot(access_token=os.environ["HUBSPOT_TOKEN"])

    company_name = fake.company()
    # Turn "Acme Industries AB" into "acmeindustries.com" so the domain
    # matches the company name, like it would in real life.
    domain = company_name.lower().replace(" ", "").replace(",", "")[:20] + ".com"

    company = client.crm.companies.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties={
                "name": company_name,
                "domain": domain,
                "numberofemployees": "120",
            }
        )
    )

    print(f"HubSpot company created: {company_name} (id: {company.id})")
    return company.id


def create_stripe_customer(hubspot_company_id: str) -> str:
    """Create one customer in Stripe and put them on the Pro plan."""
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

    customer = stripe.Customer.create(
        name=fake.company(),
        email=fake.email(),
        # pm_card_visa is a fake test card Stripe provides. Without a card,
        # a paid subscription would fail because there's nothing to charge.
        payment_method="pm_card_visa",
        invoice_settings={"default_payment_method": "pm_card_visa"},
        # metadata is a free-text notes field on Stripe objects. We store the
        # HubSpot id here so we can match the two systems up later.
        metadata={"hubspot_company_id": hubspot_company_id},
    )

    subscription = stripe.Subscription.create(
        customer=customer.id,
        items=[{"price": os.environ["PRICE_PRO"]}],
    )

    print(f"Stripe customer created: {customer.id}")
    print(f"Stripe subscription created: {subscription.id} ({subscription.status})")
    return customer.id


if __name__ == "__main__":
    print("Creating one test company and customer...\n")

    company_id = create_hubspot_company()
    create_stripe_customer(company_id)

    print("\nDone. Go check HubSpot (Companies) and Stripe (Customers).")
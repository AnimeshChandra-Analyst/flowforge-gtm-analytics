# FlowForge GTM Analytics

An end to end analytics engineering project built on a simulated SaaS company.

A Python simulator runs a fictional business called FlowForge inside real
tools (HubSpot free CRM and Stripe test mode). A separate pipeline then does
what a real data team would do: extract that data into BigQuery, model it with
dbt, and turn it into things the sales and customer success teams can act on.

The point of building it this way is that the data has real causes underneath
it. Accounts behave differently for reasons, so the models have something
genuine to discover rather than random noise to describe.

---

## The company

FlowForge is a fictional AI app builder sold to small and mid-sized teams.
Free users can upgrade themselves. Larger accounts go through sales.

### Plans

| Plan | Price per month | Seats | AI credits per month |
|---|---|---|---|
| Free | $0 | 1 | 50 |
| Pro | $20 | 3 | 500 |
| Team | $100 | 10 | 3,000 |
| Enterprise | $1,000 | 50 | 20,000 |

All plans are monthly and priced in USD. One `prompt_run` costs one credit.
Nobody starts on Enterprise: you only get there by winning a deal.

### Customer archetypes

Every simulated account is assigned a hidden archetype that drives how it
behaves. This is the "answer key" and the dbt models never see it.

| Archetype | Share | Employees | Behaviour |
|---|---|---|---|
| Ghost | 50% | 1 to 10 | Tries it on day one, then disappears. Stays on Free. |
| Power Solo | 20% | 1 | Heavy daily use, never invites anyone, upgrades to Pro. |
| Skeptic Exec | 15% | 100 to 500 | Logs in once or twice a week, often cancels. |
| Champion | 15% | 50 to 300 | Grows over time, invites teammates, hits limits, upgrades. |

Half of signups being Ghosts is deliberate. Most people who sign up for a free
tool never come back, and data where everyone is active would not be realistic.

### Product events

Five event types, generated daily per account according to its archetype:

`session_started`, `prompt_run` (costs 1 credit), `project_created`,
`teammate_invited`, `project_published`

Events carry an account domain and a user email, not a CRM id. That mirrors
real life, where the product database knows nothing about the CRM, and it is
why identity resolution matters in this project.

---

## Definitions

### Product Qualified Lead (PQL)

> A company is a PQL if it has **50 or more employees** and shows **at least 2
> of these 3 signals in the last 14 days**:
> - 3 or more active users
> - Used 80% or more of its monthly credits
> - Invited a new teammate

Reasoning: sales time is expensive, so only companies big enough to justify an
Enterprise contract are worth a human call. One signal can be a fluke, two
together is a pattern. Fourteen days is recent enough to reflect what is
happening now, long enough that one quiet week does not remove an account.

A 0 to 100 points score was deliberately rejected. Simple rules are easier to
defend when someone asks why a weighting is what it is.

### Account health

For paying customers only:

- **Red**: no activity in 14 days, or usage down 50% versus the previous 14
  days, or a failed payment
- **Yellow**: usage down 25% or more
- **Green**: everything else

Rules rather than a model, because customer success needs to know *why* an
account is red in order to do something about it.

### Sales pipeline

1. Qualified PQL (created automatically by reverse ETL)
2. Discovery call
3. Trial
4. Proposal sent
5. Closed Won / Closed Lost

---

## Architecture

```
THE SIMULATED WORLD
  seed_world.py       creates the starting 200 companies
  generate_usage.py   backfills 30 days of product usage
  daily_simulate.py   moves the world forward one day, every day

  writes to:  HubSpot (companies, contacts, deals)
              Stripe  (customers, subscriptions, invoices)
              BigQuery flowforge_raw.raw_usage_events
              BigQuery flowforge_sim.sim_accounts  <- answer key, off limits

THE DATA TEAM
  extract.py          HubSpot + Stripe APIs -> BigQuery raw tables
  dbt                 staging -> intermediate -> marts
  reverse ETL         health scores and PQL deals back into HubSpot
  Supabase + Lovable  the front end people actually use

  GitHub Actions runs simulate -> extract -> dbt run -> dbt test daily
```

### BigQuery layout

| Dataset | Contents |
|---|---|
| `flowforge_raw` | raw JSON from the APIs, plus product usage events |
| `flowforge_sim` | the answer key (hidden archetypes). Not declared as a dbt source. |
| `staging` | one cleaned, typed view per raw table |
| `intermediate` | joins and reusable logic |
| `marts` | the tables the dashboard and reverse ETL read |

GCP project: `flowforge-509421`, location EU.

---

## Design decisions

**Why simulate a world instead of downloading a dataset.** Static datasets
have no causes underneath them, so any "insight" is really just a description.
Here, hidden archetypes drive behaviour, which means a health score can be
checked against the truth afterwards rather than simply asserted.

**Why the answer key lives in a separate dataset.** `flowforge_sim` is
deliberately not declared as a dbt source, so no model can read it even by
accident. At the end, the marts get compared against it to show whether the
health and PQL logic actually identified the right accounts.

**Why GitHub Actions and not Airflow.** Airflow is free but is a server to run
and maintain. For a handful of scripts once a day it is unnecessary
complexity. At scale, Airflow would be the right call.

**Why a hand-written extractor and not a managed connector.** BigQuery's
native HubSpot and Stripe connectors were evaluated. Both were in preview, the
Stripe one only transfers pre-generated reports rather than pulling fresh data,
and the HubSpot one requires a private app with scopes the free plan does not
have. Writing the extractor meant learning pagination, rate limits and load
strategies firsthand. In a job, a managed tool like Fivetran or Airbyte would
usually be the better default for standard sources.

**Why full refresh.** Every extract run pulls everything and replaces the
table. At a few hundred records it takes seconds and it cannot produce gaps or
duplicates. Incremental loading would be the answer at a much larger scale,
using each object's last-modified timestamp.

**Why raw JSON in the warehouse (ELT, not ETL).** The extractor stays dumb: it
copies records whole into a `raw_json` column and adds an `extracted_at`
timestamp. All parsing and business logic happens in dbt, where it is version
controlled and testable. If a field turns out to be needed later, it is already
there and only a model has to change.

**Why staging is views and marts are tables.** Staging only renames and casts,
so there is no point storing a copy. Marts are read repeatedly by the app and
reverse ETL, so they are materialised.

**Why time is compressed.** Deals move a stage every few days rather than every
few weeks, and revenue movements are tracked weekly rather than monthly. Real
sales cycles and monthly reporting would need months of history before anything
showed up on a chart. The logic is identical either way.

**Why billing history starts at day zero.** Stripe does not allow billing that
already happened to be created retroactively, so revenue history begins when
the simulation began. Usage was backfilled 30 days. HubSpot and Stripe records
therefore all share a creation date, which is expected.

---

## Deliberately planted data problems

Real pipelines deal with messy data, so the simulator creates some on purpose.

| Problem | Where | Rate |
|---|---|---|
| Duplicate contacts under slightly different emails | HubSpot | ~5% |
| Events with no account domain | usage events | 3% |
| Events arriving up to 2 days late | usage events | 5% |
| Stripe customers with no link back to the CRM | Stripe | ~10% |
| Refunds | Stripe | not yet implemented |

The duplicates use variations like `john.smith@` and `johnsmith@` because
HubSpot rejects two contacts with the identical email, which is how duplicates
appear in real CRMs too.

---

## Things discovered while building

**HubSpot associations come back empty.** Contacts are not linked to companies
in the API response, so contacts are matched to companies on email domain
instead. This is realistic, since CRM associations are often missing or
unreliable, and it makes the identity resolution work more meaningful. When
reverse ETL creates deals, the company will need to be written explicitly.

**Deleting a Stripe customer does not delete their subscriptions or invoices.**
Eleven subscriptions and invoices survived from early test runs as cancelled
records with no customer. Stripe keeps financial history on purpose. The rule
going forward: subscriptions and invoices whose customer no longer exists are
treated as test data and excluded downstream.

**Two systems, two ideas of "delete".** HubSpot archives and hides records.
Stripe keeps the financial trail. Worth knowing before trusting either one's
record counts.

**Stripe moves fields between API versions.** The subscription reference on an
invoice now sits at `parent.subscription_details.subscription`, and
`current_period_start` and `current_period_end` live on the subscription item
rather than the subscription. Always inspect the JSON rather than assuming.

**HubSpot sends almost everything as text**, including numbers and dates, so
everything needs casting. It is also inconsistent: the last-modified property
is `hs_lastmodifieddate` on companies and deals but `lastmodifieddate` on
contacts.

**HubSpot keeps internal stage ids after renaming.** Deal stages come back as
`appointmentscheduled` and similar rather than the display names, so a seed
file mapping stage id to a readable name and sort order will be needed.

**Always left join events onto accounts, never inner join.** Accounts with no
events at all are exactly the ones most likely to churn. An inner join silently
drops them, which would hide the most important rows in a health report.

**Wrong JSON paths never error, they return NULL.** Every parsed column needs a
`count(*)` versus `count(column)` check before moving on.

---

## Scope

### Version 1

- All four archetypes, all four plans
- Three questions answered: who should sales call, which paying customers are
  about to leave, and where revenue growth is coming from
- Planted problems 1 to 3, plus refunds
- Reverse ETL creating PQL deals and pushing health scores into HubSpot
- One Lovable app

### Version 2 (later)

- Deal funnel and expansion candidate analysis
- Customer success onboarding route for Skeptic Execs
- Lost reasons and competitor analysis
- Alerts, annual billing, failed payments as a churn signal

---

## Status

**Done**

- Seed script creating 200 companies across HubSpot, Stripe and the answer key
- Cleanup script to reset the world
- 30 day usage backfill (~31,700 events)
- Extract script with pagination for both APIs
- Seven dbt staging models
- Daily simulator: usage, upgrades, downgrades, cancellations, new signups
- GitHub Actions running the whole pipeline every morning

**Next**

- Intermediate models: matching Stripe to HubSpot, subscription periods,
  daily usage per account
- Marts: revenue movements, account health, PQL list
- dbt tests and documentation
- Reverse ETL into HubSpot
- Supabase sync and the Lovable front end
- Validation of the marts against the answer key

---

## Scripts

| Script | What it does |
|---|---|
| `scripts/seed_world.py` | Creates the starting 200 companies. Run once. |
| `scripts/cleanup_seed.py` | Deletes everything the seed created. |
| `scripts/generate_usage.py` | Backfills 30 days of usage events. |
| `scripts/daily_simulate.py` | Moves the world forward one day. |
| `scripts/extract.py` | Pulls HubSpot and Stripe into BigQuery. |
| `scripts/peek_json.py` | Pretty prints one raw JSON record for inspection. |

## Running it locally

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Then create `.env` with the HubSpot service key, Stripe test key, the four
Stripe price ids, the path to the GCP service account key, and the BigQuery
project and dataset names.

```bash
python scripts/seed_world.py        # once
python scripts/generate_usage.py    # once
python scripts/daily_simulate.py    # once a day
python scripts/extract.py
cd flowforge && dbt run && dbt test
```

## Stack

Python, SQL, dbt, Google BigQuery, HubSpot API, Stripe API, GitHub Actions,
Supabase, Lovable.
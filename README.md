# FlowForge GTM Analytics

An end to end analytics engineering project built on a simulated SaaS company.

A Python simulator runs a fictional business called FlowForge inside real
tools (HubSpot free CRM and Stripe test mode). A separate pipeline then does
what a real data team would do: extract that data into BigQuery, model it with
dbt, and push the results back into the tools where sales and customer success
actually work.

The point of building it this way is that the data has real causes underneath
it. Accounts behave differently for reasons, so the models have something
genuine to discover rather than random noise to describe. Those reasons live
in a separate dataset the models cannot read, which means every model can be
checked against the truth afterwards instead of simply asserted.

---

## Repo layout

```
flowforge/      the dbt project (models, snapshots, seeds, macros)
scripts/        the simulator, the extractor and reverse ETL
.github/        the daily GitHub Actions workflow
```

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

### How plan changes happen

Upgrades, downgrades and cancellations are not random. Every day the simulator
checks how much of its plan each account has used over the last 30 days:

- pushing against the limits (80%+ of credits, or over the seat limit) makes an
  upgrade possible, with the odds set by archetype
- gone quiet on a paid plan (under 15% of credits) makes a downgrade or a
  cancellation possible

This is the most important design choice in the simulator. It is what makes
usage genuinely predictive of what happens next, so a model that spots the
pattern is finding a real cause rather than a coincidence.

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
  days
- **Yellow**: usage down 25% or more
- **Green**: everything else

Rules rather than a model, because customer success needs to know *why* an
account is red in order to do something about it.

Failed payments would be a strong third red signal, but every invoice in test
mode is paid because Stripe's test card always succeeds. Planting declines is
a Version 2 item.

### Account segment

`mart_account_overview` reduces all of the above to one label answering "what
should we do about this account?": churned, sales opportunity, at risk, watch,
dormant free, active free, healthy paying.

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
  dbt run             staging -> intermediate -> marts
  dbt snapshot        records subscription changes over time
  reverse_etl.py      health, usage and PQL deals back into HubSpot
  Lovable app         the front end people actually use

DAILY, VIA GITHUB ACTIONS
  simulate -> extract -> dbt run -> dbt snapshot -> reverse ETL -> dbt test
```

### BigQuery layout

| Dataset | Contents |
|---|---|
| `flowforge_raw` | raw JSON from the APIs, plus product usage events |
| `flowforge_sim` | the answer key (hidden archetypes). Not declared as a dbt source. |
| `staging` | one cleaned, typed view per raw table |
| `intermediate` | joins and reusable logic |
| `marts` | the tables the app and reverse ETL read |
| `snapshots` | subscription history (slowly changing dimension) |
| `seeds` | plan reference data |

GCP project: `flowforge-509421`, location EU.

### The models

**Staging** (7 views, one per raw table): `stg_hubspot__companies`,
`stg_hubspot__contacts`, `stg_hubspot__deals`, `stg_stripe__customers`,
`stg_stripe__subscriptions`, `stg_stripe__invoices`,
`stg_product__usage_events`. Parsing, casting and renaming only, no decisions.

**Intermediate** (3 views):

- `int_account_identity` — which HubSpot company, Stripe customer and product
  domain are the same business
- `int_account_daily_usage` — one row per account per day
- `int_account_subscription` — current plan, price and status per account

**Marts** (4 tables):

- `mart_account_health` — red / yellow / green per paying account
- `mart_product_qualified_leads` — accounts worth a sales call
- `mart_revenue_movements` — weekly new, expansion, contraction and churn
- `mart_account_overview` — one row per account with everything: plan, MRR,
  usage, limits, lifetime revenue, tenure, health, PQL status and segment.
  This is what the app reads, and what any summary is aggregated from.

**Snapshot**: `snap_subscriptions` records plan, price and status changes over
time, so "what was this account paying last week?" is answerable.

**Seed**: `plans.csv` maps Stripe price ids to plan names, ranks, seat limits
and monthly credits.

---

## Design decisions

**Why simulate a world instead of downloading a dataset.** Static datasets
have no causes underneath them, so any "insight" is really just a description.
Here, hidden archetypes drive behaviour, which means a health score can be
checked against the truth afterwards rather than simply asserted.

**Why the answer key lives in a separate dataset.** `flowforge_sim` is
deliberately not declared as a dbt source, so no model can read it even by
accident. The marts are compared against it afterwards, in the BigQuery
console, never inside dbt.

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

**Why there is a snapshot as well as full refresh.** Those are two different
jobs. Full refresh is the right way to load current state, but it means
yesterday's state is overwritten and gone. A dbt snapshot separately records
what changed, closing off the old row with a valid_to date and opening a new
one. Without it, revenue expansion and contraction would be unknowable, since
an account's previous price disappears the moment they upgrade.

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

**Why usage is stored sparse, not dense.** `int_account_daily_usage` only has
rows for days where something happened. Health and PQL logic sums over date
windows, where a missing day naturally contributes zero, so the rows are not
needed. Dense would mean thousands of rows of zeros that every downstream
model would have to filter out. Filling gaps for charting is a presentation
concern and belongs in the mart that feeds the chart.

**Why usage is dated by `occurred_at`, not `ingested_at`.** Around 5% of
events arrive up to two days late. Business questions are about when usage
happened. Ingestion time matters for pipeline decisions like what to
reprocess, not for reporting.

**Why plan details live in a dbt seed.** `plans.csv` is small, static,
manually maintained reference data, and both the subscription model and the
PQL rule need it, so keeping it in one version controlled place beats
hardcoding a case statement in two models.

**Why unattributable events are dropped.** Around 3% of events have no account
domain and are excluded by an inner join in `int_account_daily_usage`. They
could be reported separately as a data quality metric.

**Why `is_active` is derived once.** Stripe has several statuses (active,
canceled, past_due, unpaid, trialing). Downstream models only care whether the
account is paying right now, so that rule is defined once in
`int_account_subscription` rather than repeated everywhere.

**Why MRR is two separate columns.** `monthly_amount_usd` is what a
subscription is worth, including cancelled ones, because that stays a true
historical fact. `current_mrr_usd` zeroes out anything inactive and is the
column to sum for actual revenue. Cancelled accounts carrying a price is the
kind of detail that quietly inflates a dashboard number for months.

**Why reverse ETL writes the company id onto each deal.** HubSpot associations
come back empty for this account, so each deal carries
`flowforge_company_id` as a property. That is what lets the script read
existing deals and skip accounts that already have one, instead of creating a
fresh deal every morning for the same 24 PQLs.

**Why company updates are batched.** HubSpot accepts 100 records per batch
call, so 230 accounts is 3 API calls rather than 230. Faster, and much kinder
to the rate limit.

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
unreliable, and it makes the identity resolution work more meaningful.

**Deleting a Stripe customer does not delete their subscriptions or invoices.**
Eleven subscriptions and invoices survived from early test runs as cancelled
records with no customer. Stripe keeps financial history on purpose. The rule
going forward: subscriptions and invoices whose customer no longer exists are
treated as test data and excluded downstream. They drop out naturally because
the models start from HubSpot companies.

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
`appointmentscheduled` and similar rather than the display names, so reverse
ETL has to write the internal id, not the label.

**Always left join events onto accounts, never inner join.** Accounts with no
events at all are exactly the ones most likely to churn. An inner join silently
drops them, which would hide the most important rows in a health report. This
showed up concretely: 50 Ghost accounts signed up before event tracking began
and have no events at all, so an inner join made them vanish entirely.

**Wrong JSON paths never error, they return NULL.** Every parsed column needs a
`count(*)` versus `count(column)` check before moving on. A typo in a metadata
path produced a column that was 100% empty and looked exactly like the planted
missing-link problem.

**Identity resolution worked on the planted problem.** 203 of 225 accounts
matched directly on the id Stripe stores in its metadata. The remaining 22 had
no link at all and were recovered by matching on company name, giving full
coverage.

**The health model was validated against the hidden archetypes.** Every
account flagged red was a Skeptic Exec, the archetype designed to go quiet
before cancelling, and no Champion or Power Solo was ever flagged red. Of the
five paying Skeptic Execs, four came out red or yellow. The model only ever
saw credit usage across two 14 day windows and never had access to the
archetypes.

**The PQL model found 24 leads, all of them Champions.** Zero false positives.
Champions are the archetype designed to grow, invite teammates and burn
through credits. Power Solos are excluded by the 50 employee rule and Ghosts
generate almost no usage, both of which is correct behaviour rather than luck.

**Two 14 day windows have to be equal length.** An earlier version compared
the last 14 days against a 16 day window, which makes usage look like it
dropped even when it was flat.

**A case statement cannot reference an alias defined in the same select.**
The fix is a separate CTE that does the coalescing first, which also keeps
the health rules readable.

**The first day of the daily simulator produced 19 upgrades.** Accounts had 30
days of backfilled usage but no chance to act on it until the simulator
started, so every account already over its limits rolled the dice at once.
It settles from the second day.

---

## Current state

As of 29 September 2026, a few days into the daily simulation:

| Segment | Accounts | MRR |
|---|---|---|
| Sales opportunity (PQL) | 24 | $1,220 |
| Healthy paying | 34 | $1,160 |
| Watch | 7 | $300 |
| At risk | 2 | $40 |
| Churned | 5 | $180 (at time of cancellation) |
| Active free | 55 | $0 |
| Dormant free | 103 | $0 |

230 accounts, $2,720 MRR, and 45% of the customer base signed up and never
came back, which is the Ghost archetype behaving exactly as designed.

---

## Known limitations

**Name matching is easier here than in real life.** The fallback match works
because the simulator writes identical company names to HubSpot and Stripe.
Real data would have "EmberWorks", "Ember Works AB" and "emberworks" for the
same company, and would need normalising (lowercase, strip punctuation and
company suffixes) before matching.

**Active users is approximated from daily aggregates.** The PQL model takes
`max(active_users)` across 14 days rather than a true distinct count over the
window, so three people each using the product alone on different days counts
as one rather than three. Counting distinct users from the raw events would be
more accurate.

**Accounts younger than 28 days have an empty comparison window**, so their
health status is based on incomplete history.

**Revenue history starts when the simulation started.** Stripe does not allow
backdated billing, so week over week revenue comparisons only become
meaningful after a few weeks of the daily job running. Expansion and
contraction specifically depend on the snapshot, which only records changes
from the day it was first run.

**Churned MRR uses the plan price at cancellation, not at peak.** An account
that downgraded from Team to Pro before leaving is recorded as $20 of churn
rather than the $100 it once paid. The snapshot could give the more accurate
figure once it has enough history.

**The snapshot only sees one change per day.** If an account went free to pro
to team in a single day, the snapshot would capture free to team and lose the
middle step. Not an issue at this cadence, but it is why snapshot frequency
matters in real systems.

---

## Scope

### Version 1

- All four archetypes, all four plans
- Three questions answered: who should sales call, which paying customers are
  about to leave, and where revenue growth is coming from
- Planted problems 1 to 3
- Reverse ETL creating PQL deals and pushing account data into HubSpot
- One Lovable app

### Version 2 (later)

- Refunds and failed payments, the latter as a churn signal
- Deal funnel and expansion candidate analysis
- The simulator playing the sales rep, moving deals through stages
- Customer success onboarding route for Skeptic Execs
- Lost reasons and competitor analysis
- Alerts and annual billing

---

## Status

**Done**

- Seed script creating 200 companies across HubSpot, Stripe and the answer key
- Cleanup script to reset the world
- 30 day usage backfill (~31,700 events)
- Extract script with pagination for both APIs
- Seven dbt staging models, three intermediate models, four marts
- Plan reference data as a dbt seed
- Subscription history as a dbt snapshot
- Daily simulator: usage, upgrades, downgrades, cancellations, new signups
- Reverse ETL: 14 account fields written to every company, plus automatic
  deal creation for new PQLs with duplicate prevention
- Health and PQL models validated against the hidden archetypes
- GitHub Actions running the whole pipeline every morning

**Next**

- dbt tests and documentation
- The Lovable front end
- The simulator playing the sales rep, so deals move through the pipeline

---

## Scripts

| Script | What it does |
|---|---|
| `scripts/seed_world.py` | Creates the starting 200 companies. Run once. |
| `scripts/cleanup_seed.py` | Deletes everything the seed created. |
| `scripts/generate_usage.py` | Backfills 30 days of usage events. Run once. |
| `scripts/daily_simulate.py` | Moves the world forward one day. |
| `scripts/extract.py` | Pulls HubSpot and Stripe into BigQuery. |
| `scripts/reverse_etl.py` | Pushes marts back into HubSpot. |
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

# then daily, in this order
python scripts/daily_simulate.py
python scripts/extract.py
cd flowforge && dbt run && dbt snapshot && dbt test && cd ..
python scripts/reverse_etl.py
```

Order matters: the snapshot and reverse ETL both read models, so `dbt run` has
to come first.

## Stack

Python, SQL, dbt, Google BigQuery, HubSpot API, Stripe API, GitHub Actions,
Lovable.
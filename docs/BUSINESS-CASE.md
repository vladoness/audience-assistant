# Business case

*For the assumed customer in [CUSTOMER-CONTEXT.md](CUSTOMER-CONTEXT.md): a mobile operator with
about two million lines and six campaign managers running about 40 campaigns a month. Every number
below is an assumption, kept in [`business_case.yaml`](business_case.yaml); the pilot is what
measures them. `make business-case` recomputes everything, and
`make business-case ARGS="--set extra_campaigns_per_month=2"` changes one assumption in the base case.*

## Bottom line

- **Revenue carries the case.** In the base case the benefit is about **$185k a year**, of which
  about $156k is extra campaign margin. On about $50k of delivery, payback is **about three months**.
- **The analyst-hours saving is real but is not the case.** On its own it pays back in about five
  years, and it frees capacity rather than cutting cost.
- **Break-even is low.** Year one is covered if the assistant leads to about **two extra campaigns a
  month** at about $2k of margin each, before counting anything else.
- **Two numbers decide it, and the pilot measures both:** whether marketers really run more campaigns
  when an audience takes minutes, and what a campaign is worth against a holdout group.

## The value, ranked

Revenue first, then non-personnel cost and risk, then people's time. The order matters: a revenue
case can be proven in a pilot, while a productivity case only becomes money if someone's job
changes.

### 1. Revenue: campaigns on time, and more of them

- **Time-sensitive campaigns launch on time.** Month-end top-up reminders, roaming offers before a
  holiday and counter-offers lose value with every day of delay. Today each audience is a 1–3 day
  ticket.
  - **Formula:** campaigns × share that is time-sensitive × value per campaign × share of value lost
    to the wait.
  - **Base inputs:** 40 × 25% × $2k × 25% per month, which is **about $60k a year**.
- **More campaigns and tests.** When an audience takes minutes, marketers run campaigns and A/B
  tests they now skip. Contact-frequency rules cap this, so the base case assumes 4 extra a month:
  **about $96k a year**.
- **Value per campaign** is reachable lines × extra response rate against a holdout × margin per
  responder: 40,000 × 1% × $5 = **$2k** in the base case. This is the number the pilot must replace
  with a measurement.

### 2. Cost and risk: consent breaches, and messages to the wrong people

- **Consent.** An operator messaging its own subscribers pays almost nothing per SMS, so wasted sends
  are cheap. A message without consent is not. Taking Peru as the example jurisdiction:
  - In March 2026 Indecopi upheld a fine of **10.44 UIT (S/ 57,420, about $15k)** on a mobile
    operator for 22 promotional texts to one consumer without consent
    ([Indecopi](http://www.gob.pe/institucion/indecopi/noticias/1370213-indecopi-confirma-multa-a-empresa-de-telefonia-por-enviar-publicidad-sin-consentimiento)).
  - Under the personal data protection law (Ley 29733, art. 39), a very serious infraction is
    fined up to **100 UIT, about $150k**
    ([text](https://www.smv.gob.pe/Uploads/Ley_29733_vigente_2025.pdf)).
  - In 2025 the data protection authority fined two universities 25.5 and 11.25 UIT for SMS and
    email marketing without consent
    ([El Peruano](https://elperuano.pe/noticia/278302-tus-clientes-dieron-permiso-sin-consentimiento-tus-campanas-publicitarias-son-ilegales-advierte-anpd)).

  Here the consent filter is applied by the engine on every count, and consent fields can't be
  used as targeting criteria, so a hand-written query that forgets it can't reach a campaign.
  The case counts this as **expected penalties avoided, $15k a year in the base case**. That is
  risk reduction, not cash.
- **Guessed definitions.** In the demo data, 8% of lines are billed as current but made no call, SMS
  or data session in a month. A campaign that guesses the billing meaning of "active" sends 1
  message in 12 to someone not using the network. That wastes contact-frequency slots, invites
  opt-outs and makes the campaign's measured response look worse than it was. The assistant asks
  instead of guessing. This isn't counted in the totals; the pilot can count it.

### 3. People's time: real, smallest, and not a headcount cut

About 40 tickets × 1.5 hours, plus an hour of rework on the third that go back and forth, is
**about 73 analyst hours a month**, close to half an analyst: **about $14k a year** at local
salaries. The point is to move that half analyst onto analysis (campaign measurement, the churn
model), not to remove the role. The campaign managers' gain is lead time, which is already counted
under revenue.

## Costs

| | Estimate |
|---|---|
| Delivery, once | About 8 FDE weeks, roughly $50k. Weeks 1–2: connect the profile store and build the catalog from its schema. Weeks 2–4: workshops with Marketing Analytics and Legal to write the glossary and policies. Weeks 4–8: shadow pilot with two marketers and holdout groups |
| Running, per year | About $4k: hosting about $1.8k; model calls under $5 (about 3 calls per audience at $2–3 per 1,000); a few hours a month of the data owners' time settling definitions |

## Scenarios

| | Low | Base | High |
|---|---|---|---|
| Value of one campaign (extra margin) | $400 | $2k | $4k |
| Revenue: time-sensitive campaigns on time | $3k | $60k | $269k |
| Revenue: extra campaigns and tests | $0 | $96k | $384k |
| Consent penalties avoided (expected) | $5k | $15k | $30k |
| Analyst time redeployed | $6k | $14k | $26k |
| **Benefit per year** | **$14k** | **$185k** | **$709k** |
| Payback on delivery | ~5.1 years | ~3 months | under a month |
| Payback from analyst time alone | ~25.0 years | ~5.1 years | ~2.2 years |
| Extra campaigns a month that alone cover year 1 | 11.2 | 2.2 | 1.1 |

The low case is what happens if marketers don't change how much they run and a responder is worth
only $2: it takes about five years to pay back, so the pilot has to test the behaviour change and
the margin, not just the tool. A middle reading a cautious buyer might pick, $1.50 a responder
and one extra campaign a month (`make business-case ARGS="--set margin_per_response_usd=1.5
--set extra_campaigns_per_month=1"`), gives about $54k a year and a payback of about a year.

## What moves the answer

Total benefit with one assumption at its low or high value and everything else at base:

| Assumption | Low | High |
|---|---|---|
| Margin per responder | $91k | $341k |
| Extra campaigns per month | $89k | $281k |
| Extra response rate vs holdout | $107k | $185k |
| Value a late campaign loses | $149k | $221k |
| Share of campaigns that are time-sensitive | $161k | $209k |
| Expected consent penalties avoided | $175k | $200k |
| Analyst cost per month | $182k | $190k |
| Analyst hours per ticket | $181k | $188k |
| Rework hours per disputed ticket | $182k | $186k |

The top three are behaviour and campaign value. The analyst assumptions barely move it, which is
why the productivity argument shouldn't lead.

## What the pilot measures

The pilot in [CUSTOMER-CONTEXT.md](CUSTOMER-CONTEXT.md) adds the money measures to the quality ones.
Capture the baseline before it starts:

- **Value per campaign:** a holdout group on every pilot campaign, so the extra margin is measured,
  not assumed. This replaces the biggest assumption. The app already records one at every
  approval (10% by default, `measurement` parameter set), so the measurement is built in rather
  than bolted on. The demo's synthetic campaign history shows the shape of the answer: measured
  lift per campaign type, from about +0.2 points (app adoption) to about +1.6 (top-up reminders).
  The pilot replaces those with the customer's own campaigns: each pilot campaign's responses go
  to the outcome endpoint, its measured lift joins the evidence, and the learning curve shows
  when the estimate is tight enough to replace this document's response assumption.
- **Campaigns and tests per month,** pilot marketers against the others.
- **Launch delay** for time-sensitive campaigns, request to send.
- **Policy and consent blocks caught** (already counted in Insights), as risk evidence.
- **Analyst hours** spent on audience tickets.

## How I'd say it to the CMO

"About $150k a year more campaign margin, consent risk designed out, payback in about a quarter,
if the pilot confirms a campaign is worth around $2k. We'll know in eight weeks, because every
pilot campaign has a holdout group."

## Caveats

- The demo data's revenue per line (about $15 a month for prepaid) is higher than a typical
  Andean prepaid base, so campaign value is set conservatively instead of being derived from it.
- "Value a late campaign loses" and "extra campaigns per month" are judgement calls; they are the
  first things a customer's own numbers should replace.
- The consent figure is an expected value, not a forecast: it is the size of a fine times an
  assumed likelihood, and the real cost of a breach includes reputation, which this doesn't price.

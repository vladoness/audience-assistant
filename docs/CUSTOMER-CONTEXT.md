# Customer context and value

*The customer, team and figures below are assumed for the exercise. They are modelled on
patterns I have seen working with mobile operators, not on any real company's data.*

## The customer

**Andino Telecom** (fictional): a mid-size mobile operator with about two million lines,
~73% prepaid, five commercial regions. It already has a customer profile store fed nightly from
billing, the charging system, CRM and the self-care app, which is the kind of unified profile a
CDP provides.

## The team and the workflow

**Marketing Operations**: six campaign managers who run ~40 targeted campaigns a month (top-up
reminders, plan upgrades, roaming offers, app adoption). They are not technical. They know the
business ("lapsed prepaid in the Coast who still use the app") but not the column names, the
null rules, or which fields policy forbids.

Today each audience is a ticket to the data team:

1. The marketer writes the request in a ticket, in plain language.
2. An analyst interprets it, writes SQL and replies with a count. This takes 1–3 days.
3. About a third of requests go back and forth over definitions: what counts as "active"? Does
   "high value" mean the 40 USD rule or the top 10%?
4. Mistakes are expensive and silent: an audience built on the wrong "active" definition, or one
   that skipped the consent filter, means wasted SMS spend at best and a regulatory complaint at
   worst.

## What the assistant changes

The marketer types the request, sees the exact definition in plain English, answers at most one
clarifying question, sees the reachable size and approves. Then they do it again, in minutes.

| | Today | With the assistant |
|---|---|---|
| Request → sized audience | 1–3 days | under a minute |
| Definition disputes | resolved over the ticket thread | resolved once, in the glossary, by an owner |
| Consent / policy | depends on the analyst remembering | enforced by the system, not by the request |
| Audit | the SQL lives in someone's notebook | every audience records who asked, what the AI proposed, what was edited, who approved |

## The value, in the customer's terms

- **Speed to market.** Campaigns launch the day the idea comes up, not three days later. For
  time-sensitive offers (roaming before holidays, top-up reminders before month-end) that is the
  difference between running the campaign and not.
- **Analyst capacity back.** At ~40 audiences a month and 1–2 analyst hours each, that is
  roughly one analyst-week a month returned to analysis instead of ticket SQL.
- **Fewer wrong audiences.** The two error classes that matter (wrong definition, missing
  consent) are designed out rather than reviewed out.
- **A governance asset as a side effect.** Every unresolved definition becomes a visible,
  owned decision ("active": decision D-030 open, Marketing Analytics). That is the backlog the
  business needs to work through anyway.

## How I would measure it in a pilot

Two marketers, two weeks, shadow mode first (the assistant runs alongside the ticket process):

- **Agreement**: did the assistant's audience match the analyst's? (Set equality, the same
  metric as the eval harness.)
- **Time** from request to approved audience.
- **Clarification rate** per term, which tells the business which definitions to settle first.
- **Edit rate**: how often marketers change the AI's proposal before approving. A high rate on
  one field is a prompt or glossary fix.

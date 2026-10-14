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

## The decisions it supports

Two people make decisions here, and the tool supports both without making either:

| | Campaign manager | Marketing Analytics (definition owner) |
|---|---|---|
| Decision | Whom this campaign targets, under which definition | What "active", "likely to churn" and other contested terms mean, for everyone |
| What they see before deciding | The exact definition in plain English; for each answer to an open term, the lines it reaches and what only it adds; policy blocks with the policy named; how recently these lines were contacted; what past campaigns of this type achieved against their holdouts | How often each open term is asked about and which answer marketers chose (Insights) |
| What they do | Answer, edit, change in words, approve with a goal | Settle the term in the glossary; affected approved audiences are flagged with their size under the new rule |
| How we know it was right | The holdout recorded at approval: once the campaign tool reports responses, the measured lift is compared with what the evidence predicted, and the campaign joins the evidence for the next one of its type | Fewer questions, fewer edits, and approved audiences that measure well |

## What the assistant changes

The marketer types the request, sees the exact definition in plain English, answers a question
only when a business term is genuinely ambiguous, adjusts it in words if needed ("also the
North", "make it email"), sees the reachable size and approves. Then they do it again, in minutes.

| | Today | With the assistant |
|---|---|---|
| Request → sized audience | 1–3 days | under a minute |
| Definition disputes | resolved over the ticket thread | resolved once, in the glossary, by an owner |
| Consent / policy | depends on the analyst remembering | enforced by the system, not by the request; the blocking policy is named |
| Audit | the SQL lives in someone's notebook | every audience records the request, what the AI proposed, what was edited, who approved |

## The value, in the customer's terms

In money, the base case is about **$185k a year** of benefit for about $50k of delivery, a payback
of about three months. Extra campaign margin carries it; the analyst time on its own would take
about five years to pay back. Assumptions, scenarios and what moves the answer are in
[BUSINESS-CASE.md](BUSINESS-CASE.md).

- **Speed to market.** Campaigns launch the day the idea comes up, not three days later. For
  time-sensitive offers (roaming before holidays, top-up reminders before month-end) that is the
  difference between running the campaign and not.
- **Analyst capacity back.** At ~40 audiences a month and 1–2 analyst hours each, that is
  40–80 hours a month (a quarter to half of one analyst) returned to analysis instead of ticket
  SQL. On its own that is a modest saving; speed to market is the bigger lever.
- **Fewer wrong audiences.** Missing consent and forbidden data are blocked in code. Ambiguous
  definitions are no longer silently guessed: they are asked about, and the definition used is
  shown in plain English before approval.
- **A governance asset as a side effect.** Every unresolved definition becomes a visible,
  owned decision ("active": decision D-030 open, Marketing Analytics). That is the backlog the
  business needs to work through anyway.

## How I would measure it in a pilot

Two marketers, eight weeks, in two phases. Capture the baseline first: today's
request-to-audience time and how often definitions are disputed.

**Phase 1, weeks 1–2, shadow mode:** the assistant runs alongside the ticket process and nothing
it builds is sent. This measures quality and speed safely, but it can't measure money: a campaign
that isn't sent has no responders.

**Phase 2, weeks 3–8, live:** the two marketers send the audiences they approve in the app, each
with its holdout, and the campaign tool reports outcomes after each 14-day window. This is where
the business case's biggest assumption, the value of a campaign, gets measured.

Measured across both phases:

- **Agreement**: did the assistant's audience match the analyst's? (Set equality, the same
  metric as the eval harness.)
- **Time** from request to approved audience.
- **Clarification rate** per term, which tells the business which definitions to settle first.
- **Edit rate**: how often marketers change the AI's proposal before approving (answering a
  clarification doesn't count). A high rate on one field is a prompt or glossary fix.
- **Where the work starts**: if marketers already plan campaigns in an assistant, drafts arrive
  through the MCP server and are approved in the web app; if not, the web app alone is enough and
  the MCP route waits.

**Go / no-go after the pilot:**
- **Go** if at least 95% of audiences match the analyst's, no approved audience breaks a policy,
  the median request-to-approval time is under 5 minutes, and the pilot campaigns' confirmed
  outcomes show lift clearly above zero for at least one campaign type (the measured lift then
  replaces the business case's response assumption, whatever it turns out to be).
- **Fix the catalog or glossary first** if one field or term drives most of the mismatches.

# Reference: evals, tests, endpoints, operations

Detail behind the short sections of the [README](../README.md).

## Eval results in full

**Measured** (prompt v7, refinement r2, advisor a2). Every set was re-run live on 15 Oct 2026
after the final review moved negation into the model's output (prompt v7) and added nine negation
cases. Every configuration was run 3 times. Prices are from Anthropic's list effective 2026-10-07.
Latency is the model call, nearest-rank percentiles. The interval under each pass rate is a 95%
Wilson interval over cases (a case counts when every run passed): it says how far a set this small
can be trusted, and it is wide.

| | **Sonnet 5.5 · low effort (default)** | Haiku 5.5 · low effort | Haiku 5.5 · default effort |
|---|---|---|---|
| Development set, 43 cases × 3 runs | **100%** (92–100%) | 98% (88–100%) | 97% (85–99%) |
| Held-out set, 18 cases × 3 runs | **100%** (82–100%) | 89% (67–97%) | 93% (67–97%) |
| Refinement set, 17 cases × 3 runs | **100%** (82–100%) | 94% (73–99%) | not run |
| Advisor set, 12 cases × 3 runs (known-truth history) | **92%** of runs (33 of 36); 10 of 12 cases every run (55–95%) | 97% of runs (35 of 36); 11 of 12 cases (65–99%) | not run |
| Latency p50 / p95 / max (dev) | 2.0 s / 3.9 s / 15.1 s | 1.4 s / 2.2 s / 3.5 s | 1.7 s / 3.2 s / 4.2 s |
| Latency p50 / p95 / max (refinement) | 1.8 s / 2.2 s / 7.2 s | 1.5 s / 2.5 s / 2.7 s | not run |
| Latency p50 / p95 (advisor) | 4.3 s / 6.1 s | 2.7 s / 5.9 s | not run |
| Cost per 1,000 requests (dev / refinement / advisor) | $1.92 / $2.67 / $8.53 | $0.11 / $0.18 / $0.50 | $0.16 / not run / not run |

The sets are small enough that Sonnet's 100% and Haiku's 89–98% overlap at 95%: they show
regressions and the kinds of mistakes, not a precise accuracy. The 15.1 s and 7.2 s maxima are
single slow calls (the SDK's retry after a timeout); they are why the timeout and breaker exist.

**What the advisor eval shows, including what it doesn't.**
- **Against three baselines that use no model:** "always keep the audience as it is" passes 42%
  of the advisor cases; a twenty-line rule passes 75%; the same rule with a keyword list for the
  goal passes 83%. Sonnet passes 92% of case-runs, Haiku 97%.
- **Where the model beats the rules:** reading a goal that doesn't name the type ("used to
  recharge every week and have gone quiet; bring them back" is a win-back, which both rules miss).
- **Where it doesn't, and what caught it:** for "They keep hitting their limit before the month
  is over", Sonnet proposed a top-up reminder for a postpaid audience in 2 of 3 runs. The
  eligibility check withheld it (top-ups are for lapsing prepaid lines), so nothing wrong reached
  the marketer, but nothing right did either. The checks withheld 9 of Sonnet's 87 proposals and
  2 of Haiku's 60; scored without them, Haiku drops from 97% to 94%.
- **On the structured cases, the rules do as well.** Those cases test that nothing misleading
  reaches the marketer, and the model's other contribution, explaining in plain language, isn't
  scored. The honest reading: the advisor's measurable value is goal reading; its proposals are
  worth checking, not trusting.

The advisor costs under a cent a call; at this customer's volume that is about 35 cents a month.

"Pass" means every expectation for that case held: the right outcome status, **exactly the same
set of reachable lines as the expected audience** (for a refused policy case, the part left after
the refusal), the right clarification or unmapped phrase, and policies named and enforced.
Per-case results are in `evals/results/`. Run 1 of every configuration is recorded, so
`make eval-replay MODEL=... EFFORT=...` reproduces its outcomes offline, misses included (the
advisor set replays at 10 of 12). Three Sonnet dev requests
share their text with the UI examples and were re-recorded with them: same outcomes, different
latencies.

**What the misses were.** Sonnet missed none of the 234 interpretation and refinement case-runs
(43, 18 and 17 cases × 3 runs)
in these runs, including all nine negation cases ("not active", "inactive", "other than high
value", "neither … nor", Spanish "no están activos", and "No roaming high value", where the
negation belongs to roaming). In an earlier run it missed one: it read "haven't recharged in 60
days" as at least 60 rather than more than 60, and its summary said "at least 60 days", so the
marketer could see the reading. Treat it as 99–100%, not as perfect. Haiku's misses (one
Haiku-default miss is a rate-limited call, counted as a failure):
- **Governed term applied without evidence.** "iOS users" picked up the governed *app users*
  (held-out, every run at low effort; with an email campaign it then got blocked by MKT-12), and
  "spending more than 60 dollars a month" became the governed *high value* (revenue ≥ 40) instead
  of the marketer's own 60 (dev, every run at low effort). The deterministic grounding check
  flagged both to the marketer.
- **A policy-blocked field listed as missing data.** At default effort, "medium credit risk band"
  was put under "no data" in one run of three instead of being named as blocked by MKT-07. The
  audience was right, and the field was not used.
- **The same boundary ambiguity** (≥ 60 against > 60), in every run at default effort.
- **Over-caution with PII.** In a request containing a contact email, it asked the marketer to
  confirm dropping "contact [EMAIL] for details". The audience was right.
- **"Also" on a field the audience doesn't filter.** On the refinement set Haiku read "also
  include the Highlands", for an audience with no region condition, as "only the Highlands", in
  every run. The What changed box shows it ("+ Region is Highlands"), but it narrows the audience.
  Sonnet got it right every time.

**Decision: Sonnet 5.5 at low effort.** At this customer's volume (about 40 audiences a month)
the cost difference is under a dollar a month. The accuracy difference is between Sonnet, right
in every interpretation case-run of these runs (and in all but one of 207 in an earlier run), and
Haiku, whose misses the marketer has to catch from the warnings. Haiku at low effort is the documented option for high-volume tenants: about 17× cheaper and a little
faster, with every governed-term miss surfaced by the grounding check. The configuration is one
environment variable (`ANTHROPIC_MODEL`, `ANTHROPIC_EFFORT`).

**How the prompt got here (honest log).**
- **First live run.** The model replaced "more than 15 GB" with the governed term. I added a
  rule that the marketer's own number wins, plus a dev case for it.
- **A false claim I corrected.** The first version of this README called that case one the model
  "had not seen", but the prompt's own example used the same phrase. An external review caught
  it.
- **Separating dev from held-out.** I then rewrote the prompt examples so none mirrors an eval
  case. I wrote a separate 18-case held-out set after the prompt was frozen and never changed the
  prompt because of it.
- **Caveat.** I did use both sets to choose the default model, so these figures are not an unbiased
  estimate either. A fresh held-out set, built from the customer's real requests during a pilot,
  is the next step.
- **Prompt fixes are fragile.** Changing the example re-broke the 15 GB case on Haiku. That is
  exactly why the safety-relevant checks live in code, not in the prompt.
- **Refinement.** I wrote the refinement instructions and their 16 cases together; all passed on
  both models first time. Recording the UI's suggested changes then exposed a misread the set
  didn't cover: "also include the Highlands" on an audience without a region filter narrowed it.
  I added the case and one sentence to the instructions (r1 → r2). Sonnet now passes it; Haiku
  doesn't. The refinement set is a development set, not a held-out one.

**How I'd explain the choices to the customer:**
- **Accuracy is a system property.** The model can misread a request. It cannot get an audience
  approved that:
  - breaks a data-usage policy;
  - ignores consent;
  - uses a field that doesn't exist;
  - answers an open business term without the marketer choosing.

  Those are blocked in code. A governed definition the model replaced with its own condition is
  flagged, not blocked, because the marketer's own number is allowed to win. The residual risk
  is a *valid but wrong* reading, which is why the marketer approves a code-generated summary
  and why the eval compares audiences, not JSON.
- **Latency:** sizing takes milliseconds even at 5M lines; the model call is the wait (about 2 s
  median).
  - **Mitigations:** a response cache for repeated requests, prompt caching of the static
    catalog (~3k tokens), low effort, a capped output, at most one repair call.
  - **Worst case:** about 40 s (10 s timeout, 1 retry, at most 2 calls). After 3 consecutive
    failures a circuit breaker pauses the assistant for 30 s, so people fall back to the editor
    instead of waiting.
- **Cost:** irrelevant at one team's volume; it matters at many tenants, where the Haiku option
  and model routing become worth their complexity.

## Testing and reliability

- **`make test`: 183 tests, no network.** A scripted fake model drives each failure mode
  deterministically:
  - repair once, then stop;
  - outage fallback and the circuit breaker;
  - cache hits;
  - PII never reaching the model;
  - policy-blocked conditions kept visible across a channel switch;
  - forged provenance, unanswered clarifications and double approvals rejected;
  - one request id end to end;
  - refinements (`tests/test_refine.py`): untouched conditions keep their sources, governed
    conditions move only with their term, open questions and unmapped parts carry forward and
    are counted once, an outage changes nothing, and an edit made before a change still counts
    as an edit at approval;
  - the advisor (`tests/test_advisor.py`): only the planted segment effect is flagged, segment lift
    recovers the planted truth, invented patterns and numbers are withheld (in proposals and in
    the summary), blocked channels are refused, a campaign-type switch is withheld once a type is
    chosen or when it is on another channel, consequences show fewer responders when narrowing,
    an "advisor" label survives only for a condition recorded advice proposed, and no customer
    ids, audience names, small counts or blocked fields reach the facts;
  - the learning loop (`tests/test_learning.py`): the export is the stored membership (contacted
    lines by default), reports need the token, implausible reports are rejected (dates, open
    window, wrong file, impossible rate), a pending report can be replaced and is learned from
    only once confirmed, a confirmed one can't be overwritten, the learning curve narrows, a
    difference is only claimed when intervals don't overlap, and a result that can't be told
    from zero is never "in line";
  - negation (`tests/test_review_round3.py`): "not active" is asked as an exclusion ("Not: used the
    network…"), only an exclusion answers it, even at approval; exclusions the phrase check can't
    see ("inactive", Spanish) come from the model; when the model and the phrase check disagree,
    both readings are offered; "not high value" applies the exclusion of the governed definition;
  - consent after approval (`tests/test_learning.py`): a line that withdraws consent is left out of
    the export and of both measured groups;
  - campaign history (`tests/test_history.py`): measured lift recovers the known true effect of
    each campaign type within three standard errors, no past contact lacked consent, contact
    pressure matches a direct count, and the evidence shown is the evidence recorded at approval;
  - decision support (`tests/test_decisions.py`): option consequences equal set arithmetic over
    the store, none are shown for an invalid definition, approval needs a goal, and the holdout
    is reproducible from the record and flagged when too small;
  - the MCP server (`tests/test_mcp.py`): no approve tool, drafts audited as `origin: "mcp"`,
    sizing refused while questions are open, and a draft reviewed and approved through the web
    app. A stdio round trip with the SDK's own client was checked by hand.

  SQL counts are checked against independent queries, and values are checked to be bound
  parameters. `tests/test_review_fixes.py` and `tests/test_review_round3.py` hold one regression
  test per finding from the external reviews (see "Use of AI" in the README), and `tests/test_eval_scorer.py`
  checks that the eval scorer fails wrong answers.
- **Evals.**
  - `evals/cases.yaml` is the development set (43 cases).
  - `evals/heldout.yaml` is the frozen held-out set (18 cases).
  - Both cover simple and threshold requests, decided and open glossary terms, missing data,
    vague qualifiers, policies, injection, out-of-scope, Spanish and PII.
  - `evals/refine.yaml` (17 cases) starts each case from a written-out audience plus one change:
    widen, remove, replace, channel, governed terms, carried questions, missing data, policy,
    "no change" and Spanish.
  - Scoring is by exact audience equality plus status, clarification, unmapped and policy checks.
    An "invalid" result only passes if its errors come from the request itself (a policy, scope
    or contradiction), never from the model, and, where the case gives a definition, if the part
    of the audience without errors matches it. (A review found the earlier scorer skipped those
    checks; every result below was re-run with the stricter scorer.)
  - Repeats show run-to-run variance, and flaky cases are listed in the results.
  - Each recording stores a fingerprint of the system prompt; replaying a recording made with a
    different prompt or catalog fails instead of silently reporting old results.
  - **Limitations:** the sets are small and mostly clean requests. Negation now has nine dev
    cases ("not active", "inactive", "other than", "neither … nor", Spanish, and a "No roaming
    high value" false-positive guard), but there is no separate hard tier yet (synonyms outside
    the alias lists, messy multi-sentence tickets, two audiences in one request).

- **UI walkthrough (optional, needs a browser):** `scripts/ui_walkthrough.mjs` drives the New
  audience journey in Chrome through Playwright and checks the gating at each step (24 checks;
  it runs offline, using only the example requests). Instructions are at the top of the file.

## Endpoints

- `POST /api/interpret`, `POST /api/refine` and `POST /api/advise`: the only three that call the model.
- `POST /api/check`, `POST /api/size`, `POST /api/approve`, and `POST /api/measurement-preview`
  (before approval: the holdout size, the smallest lift it would show, and overlap with recent
  approvals). Approval rejects a campaign type sent on another channel.
- `GET /api/history` and `GET /api/history/evidence/{campaign_type}`: campaign types with past
  campaigns, and their measured lift (read-only).
- `POST /api/audiences/{id}/outcome` (needs `X-Outcome-Token`): the data team reports
  responders (from billing records) after the window; the lift is measured against the membership stored at approval
  and kept as pending. `POST /api/audiences/{id}/outcome/confirm`: a person confirms it, and only
  then does it join the evidence. `POST /api/audiences/{id}/simulate-outcome` does the same with
  generated responses, demo only.
- `POST /api/advise`: the campaign advisor (model proposals, checked and sized by code).
- `GET /api/learning` (the loop in numbers and the expected-against-measured scorecard) and
  `GET /api/learning/{campaign_type}` (the learning curve).
- `GET /api/audiences/{id}/export.csv`: the approved lines to contact, exactly as approved;
  `?group=held_out` or `?group=all` for the measurement side.
- `GET /api/interpretations/{id}`: a recorded interpretation, re-checked against today's rules,
  for review (the target of the MCP review links, `/?review=<id>`).
- `GET /api/audiences`, `GET /api/insights`, `GET /api/catalog`, `GET /api/metrics`.
- `GET /healthz` (liveness) and `GET /readyz` (readiness).

OpenAPI docs are at `/docs`.

## Observability and operability

- **Logs.** One JSON object per line, all carrying the same `request_id` that is returned in the
  `x-request-id` header, shown in the UI, and used as the key of the audit record. Key events:
  - `interpretation`: status, issue codes, cache hit, repaired, model calls, ms, cost, PII count,
    `origin` (`ui` or `mcp`), and `parent_request_id` when it is a refinement.
  - `llm_call`: OpenTelemetry GenAI names (`gen_ai.request.model`, `gen_ai.usage.input_tokens`,
    …), latency and cost.
  - `unhandled_error`, with the stack trace.
  - `llm_circuit_opened`, `refine_unavailable`, `audience_approved`, `http_request`.
- **Metrics** (`GET /api/metrics`):
  - counters by outcome and by HTTP status class;
  - cache hits, repairs, model calls, tokens and cumulative cost;
  - p50/p95/max per route and for the model call.
- **Health.**
  - `/healthz` is liveness: governance versions, lint receipt, measured eval accuracy for the
    active configuration, model spend vs budget.
  - `/readyz` returns 503 unless profiles are loaded and the audit store answers. It reports
    "degraded" when the assistant is paused by the budget or the circuit breaker.
- **Debugging a bad audience.** Take the request id from the UI or the header, then run
  `SELECT * FROM interpretations WHERE request_id = ?` in `data/audit.sqlite`. It holds the
  masked request, the raw model proposal, the final definition, the issues and the telemetry.
  For a refined audience, `parent_request_id` walks back through each change to the original
  request. `grep <request id>` gives the matching log lines.
- **What I'd alert on:**
  - rising `interpret_invalid`, `ai_unavailable` or `circuit_open` counts;
  - model p95 above the SLO;
  - repair rate above baseline;
  - daily cost above budget.
- **Data safety.** The server refuses to start without a profile store unless `SYNTHETIC_DATA=1`
  is set, which `make run` sets for the demo. It never silently invents data in a real deployment.

# Audience Assistant

A marketing-operations campaign manager types *"prepaid customers in the Coast who haven't
topped up in over 30 days but still use the app"*. A few seconds later they see the exact audience
definition in plain English, answer at most one clarifying question, see how many lines can
actually be reached, and approve it with a full audit trail.

The AI model translates language into a structured proposal. Everything that has a correct
answer is done by deterministic code: which fields exist, what governed terms mean, consent,
counting, and the summary the user approves.

- **Customer context and value:** [docs/CUSTOMER-CONTEXT.md](docs/CUSTOMER-CONTEXT.md)
- **Design note and diagram:** [docs/DESIGN.md](docs/DESIGN.md)

---

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
make setup        # uv sync
make run          # http://127.0.0.1:8000  (generates 200k synthetic profiles on first start, ~1 s)
make test         # 38 tests, no network or API key needed
```

**With a model (recommended):** `cp .env.example .env`, add `ANTHROPIC_API_KEY`, then `make run`.

**Without a key:** the app starts in offline mode. The example prompts replay recorded model
responses (`evals/fixtures/`). Anything else shows "assistant unavailable" and the condition
editor still works end to end. That is also the designed behaviour when the model is down.

Other targets: `make eval` (golden set against the live model), `make eval-replay` (the same,
offline from recordings), `make bench` (sizing latency at 200k / 1M / 5M lines).

---

## The workflow

1. **Describe** the audience in plain language (English or Spanish). Emails and phone numbers are
   masked before the model sees the request.
2. **Check what will run.** Each condition shows where it came from: **AI**, **governed** (a
   glossary decision with an id and owner), **your choice** (a clarification answer) or
   **edited**. The marketer can change, add or remove any condition; every change is re-validated.
   - **Ambiguous business term** (e.g. "active", "likely to churn"): one question with the
     competing governed definitions as options.
   - **Part of the request has no data behind it** (e.g. "called the call centre"): shown
     explicitly, and approval requires accepting that the audience is built without it.
3. **Size.** Matched lines, minus those without consent for the channel, gives reachable lines,
   with region and line-type breakdowns. Warnings flag empty, tiny (< 100) or very broad (> 50% of
   base) audiences.
4. **Approve.** The server re-validates and re-sizes the submitted definition, then writes an
   audit record: request (masked), the model's raw proposal, model and prompt version,
   catalog and glossary versions, whether the marketer edited it, counts at approval, and approver.

---

## Architecture

```
static/            index.html + app.js: no build step
app/main.py        FastAPI routes, request-id middleware, app factory (tests inject a fake model)
app/interpret.py   pipeline: mask → cache → model → glossary expansion → validate → repair once → status → explain
app/llm.py         the only model call: forced tool use, prompt, Anthropic / replay / recording providers, pricing
app/validate.py    catalog rules: fields, operators, types, ranges, restricted & consent fields, scope, contradictions
app/explain.py     plain-English rendering of a definition (templates, no model)
app/audience.py    definition → parameterized SQL on DuckDB; consent filter; grain check; guardrails
app/store.py       SQLite audit trail (interpretations, approved audiences) + TTL/LRU response cache
app/observability.py  JSON logs with request id; counters and latency percentiles
app/synthdata.py   deterministic synthetic profile store (DuckDB SQL)
config/            catalog.yaml (fields), glossary.yaml (governed terms), models.yaml (prices)
evals/             golden set, runner, recorded fixtures, results
```

Endpoints: `POST /api/interpret` (the only one that calls the model), `POST /api/check`,
`POST /api/size`, `POST /api/approve`, `GET /api/audiences`, `GET /api/catalog`,
`GET /api/metrics`, `GET /healthz`. The interactive OpenAPI docs are at `/docs`.

The diagram and the reasoning behind each choice are in [docs/DESIGN.md](docs/DESIGN.md).

---

## Assumptions

- **Data:** the profile store is a nightly snapshot with one row per mobile line (synthetic,
  200k lines by default). Audiences are batch-evaluated; real-time/streaming qualification is out
  of scope.
- **Governance already exists as config:** a catalog of targetable fields and a glossary of
  business terms with owners. In a real deployment, building these with the business is part of
  the engagement, and the open terms are its first backlog.
- **Logic:** an audience is an AND of conditions; OR is supported within one field (`in`). Nested
  boolean logic was left out deliberately (see limitations).
- **Users:** one tenant, trusted internal users, approver identified by name. Authentication and
  roles would come from the customer's SSO.
- **Activation** (sending the audience to an SMS/email tool) is out of scope; the approved
  definition and its hash are the hand-off.

---

## Trade-offs: latency, cost, accuracy

**Measured on the golden set** (`make eval`, 32 cases, temperature 0):

| | claude-haiku-4-5 | claude-sonnet-4-5 |
|---|---|---|
| Pass rate (all expectations met) | _TBD_ | _TBD_ |
| Audience exact match (same reachable lines) | _TBD_ | _TBD_ |
| Model latency p50 / p95 | _TBD_ | _TBD_ |
| Cost per 1,000 requests | _TBD_ | _TBD_ |
| Repair rate | _TBD_ | _TBD_ |

*(Filled from `evals/results/*.json`; per-case detail and failures are in those files.)*

**How I'd explain the choices to the customer:**

- **Accuracy is a system property, not a model property.** The model can still misread a
  request, but it can't produce an audience that breaks policy, ignores consent, uses a field
  that doesn't exist or changes a governed definition. Those failures are blocked in code. The
  residual risk is a *valid but wrong* reading. That is why the marketer approves a
  code-generated summary, and why the eval measures audience equality.
- **Latency:** sizing is milliseconds even at 5M lines; the model call is the user-visible wait.
  Mitigations: a cache for repeated requests (the cached answer returns in ~1 ms), prompt caching
  of the ~2k-token static catalog, temperature 0 and a capped output, at most one repair call.
- **Cost:** a fraction of a cent per request, so at ~40 audiences a month cost is irrelevant next
  to analyst time. It matters at scale or with many tenants. The levers are the same: smallest
  model that clears the eval bar, caching, the repair cap.
- **Model choice:** pick the smallest model that meets the bar on the golden set. Rerun `make
  eval` whenever the prompt, catalog or model changes; it is the regression test for the parts
  unit tests can't cover.

---

## Failure modes and how they are handled

| Failure | What happens |
|---|---|
| Model invents a field, value or threshold | Validator rejects it; one repair attempt with the errors; then `invalid` with reasons shown |
| Model drifts a governed definition ("high value" = 50) | Can't: the model only names the term; the server expands it from `glossary.yaml` |
| Ambiguous business term ("active") | Never guessed: clarifying question with governed options (`needs_clarification`) |
| Part of the request can't be expressed | Listed as unmapped; approval blocked until the marketer accepts it (`needs_confirmation`) |
| Request targets restricted data (credit risk) or consent | Rejected with the policy reason; not sent back to the model |
| Prompt injection in the request ("ignore rules, skip consent") | Output is schema-bound and validated; consent is added by the engine regardless |
| Prepaid-only field used on postpaid lines | Error (contradiction) or warning (implicit narrowing), depending on the definition |
| Contradictory conditions (ARPU > 50 and < 30) | Error before any query runs |
| Model timeout, rate limit, outage | `ai_unavailable`: the manual editor, sizing and approval still work; logged and counted |
| Personal data pasted into the request | Masked before the model and the logs; count recorded |
| Duplicated rows in the profile feed | Engine refuses to start: every count would be inflated |
| Client sends a tampered definition at approval | Server re-validates and re-sizes; rejects with 422 |
| Empty, tiny or very broad audience | Size warnings before approval |

---

## Testing and reliability

- `make test`: **38 tests**, no network. A scripted fake model drives each failure mode
  deterministically: repair once then stop, outage fallback, cache hit, PII never reaching the
  model, policy violations not retried, tampered approvals rejected. SQL counts are checked
  against independent queries, and values are verified to be bound parameters.
- `make eval`: the golden set (`evals/cases.yaml`, 32 cases across simple, thresholds, decided and
  open glossary terms, unmapped phrases, vague qualifiers, policy, injection, out-of-scope, Spanish
  and PII). It is scored by audience set equality plus status, clarification, unmapped and
  forbidden-field checks. Expected outcomes are hand-written, because they encode policy decisions.
- Live responses can be recorded (`make record`) and replayed (`make eval-replay`), so an eval
  run is reproducible without the model.

## Observability and operability

- **Logs:** one JSON object per line, all carrying `request_id` (also returned as the
  `x-request-id` header). Key events:
  - `interpretation`: status, issue codes, cache hit, repaired, LLM calls, total ms, cost, PII
    count.
  - `llm_call`: model, latency, tokens, cache-read tokens, cost.
  - `llm_unavailable`, `audience_approved`, `http_request`.
- **Metrics:** `GET /api/metrics`:
  - counters by outcome (`interpret_ready`, `interpret_needs_clarification`, …), cache hits,
    repairs, model calls, tokens, cumulative cost;
  - p50/p95/max for `llm_ms`, `interpret_ms`, `size_query_ms` and each route.
- **Health:** `GET /healthz` reports profiles loaded, provider, model and catalog version.
- **Debugging a bad audience:** take the `request_id` from the UI or header, then
  `SELECT * FROM interpretations WHERE request_id = ?` in `data/audit.sqlite`. It holds the masked
  request, the raw model proposal, the final definition and telemetry.
- **What I'd alert on in production:**
  - a rising `interpret_invalid` or `llm_unavailable` rate;
  - `llm_ms` p95 above the SLO;
  - repair rate above baseline (a catalog or prompt regression);
  - daily cost above budget.

## Performance and scalability

`make bench` on a laptop (Apple Silicon), median of 20 runs after warm-up:

| Lines | 1 condition | 3 conditions | 5 conditions incl. `in`/`between` |
|---|---|---|---|
| 200k | 4.5 ms | 3.0 ms | 7.6 ms |
| 1M | 5.9 ms | 4.1 ms | 9.1 ms |
| 5M | 30.4 ms | 16.8 ms | 46.7 ms (p95 55 ms) |

The bottleneck is the model call (seconds), not the data. Scaling notes (approximate counts for
previews at CDP scale, multi-tenant config, stateless replicas, model routing) are in
[docs/DESIGN.md](docs/DESIGN.md#scaling-path-not-built).

---

## Known limitations and what I left out on purpose

- **AND-only logic** (with OR inside one field). Nested groups add little for the target user
  and make the plain-English summary and the editor much harder to get right.
- **No lookalike, propensity or churn scores.** There is no approved model in the catalog, so
  the assistant offers governed proxies instead of inventing one.
- **No authentication or roles.** The approver is a typed name; real deployments use SSO.
- **No activation.** Approved audiences are not pushed to a campaign tool.
- **Single process.** The cache is in-memory and the audit store is SQLite, chosen for a
  runnable slice. Both are behind small interfaces.
- **The PII masker is regex-based.** It catches emails, phone numbers and IDs, not names.
- **The glossary and catalog are hand-authored YAML.** There is no UI for owners to settle open
  terms yet; the open terms are listed with their pending owner.
- **The golden set is 32 cases.** That is enough to compare models and catch regressions, not
  to certify accuracy.

## Use of AI in building this

<!-- EDIT THIS SECTION TO MATCH WHAT YOU ACTUALLY DID BEFORE SUBMITTING -->
I built this with an AI coding agent (Claude in Cursor) as a pair, which is how I work day to day.

- **Where AI was used:** scaffolding the project and first drafts of most modules, tests and
  docs; generating the synthetic data SQL; drafting eval cases for me to review; rubber-ducking
  design alternatives.
- **What I decided and verified myself:**
  - the workflow and customer framing;
  - the architecture rule (model proposes, code disposes);
  - glossary terms expanded server-side;
  - "never drop, always surface" for unmapped phrases;
  - status derived by the validator;
  - one repair, then stop;
  - consent enforced in the engine;
  - evaluation by audience set equality.
  I also reviewed every eval expectation and the validation rules line by line, and ran the app
  and the failure paths myself.
- **Where I deliberately did not rely on AI:**
  - **Runtime checks.** Anything with a correct answer is deterministic code at runtime: validation,
    consent, counts and the summary the user approves.
  - **Eval expectations.** The golden-set expectations are hand-checked, not model-generated:
    a model grading its own family's output against expectations it wrote would measure nothing.
  - **Approval text.** The approval summary is never written by a model.

## Extensions I would do next

1. **Owners settle open terms in the UI.** A glossary decision becomes a versioned record with
   an owner and date, and dependent audiences are flagged for re-approval.
2. **Re-size on a schedule.** Re-size approved audiences and alert on drift ("your audience
   grew 40% since approval").
3. **Model routing.** Small model first, escalate on validation failure; decide using the eval
   data.
4. **Activation hand-off** to the campaign tool, keeping the definition hash on the campaign
   record.
5. **Close the learning loop.** Feed marketers' edits back into the eval set as new cases.

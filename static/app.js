// Audience Assistant UI. Plain JS, no build step.
// The server is the source of truth: every edit is re-checked by /api/check before sizing or approval.

const OPS = {
  enum: ["eq", "neq", "in", "not_in"],
  integer: ["eq", "neq", "gt", "gte", "lt", "lte", "between"],
  number: ["eq", "neq", "gt", "gte", "lt", "lte", "between"],
  boolean: ["eq"],
};
const OP_LABELS = { eq: "is", neq: "is not", in: "is any of", not_in: "is none of", gt: ">", gte: "≥", lt: "<", lte: "≤", between: "between" };
const STATUS_TEXT = {
  ready: "Ready. Check the summary, then continue.",
  needs_clarification: "One term in your request has competing definitions. Choose the one you mean.",
  needs_confirmation: "Part of your request can't be built from the available data. Confirm before approving.",
  unsupported: "This doesn't describe an audience the profile store can build. Try rephrasing, or use the editor.",
  invalid: "The proposal broke a data or policy rule. Fix the highlighted conditions below.",
  ai_unavailable: "The assistant is unavailable right now. You can still build the audience with the editor.",
};

const state = {
  catalog: null, attrs: {}, requestId: null, status: null, definition: { conditions: [], channel: "sms" },
  pendingTerms: [], unmapped: [], acknowledged: false, sized: null, step: 1,
};

const REVIEW_HINTS = {
  needs_clarification: "Answer the question above to continue.",
  needs_confirmation: "Tick “Build the audience without this part” to continue.",
  invalid: "Fix the highlighted conditions to continue.",
  unsupported: "Rephrase the request, or add conditions in the editor.",
  ai_unavailable: "Build it with the editor below, or try again in a moment.",
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => Number(n).toLocaleString("en-US");

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { data });
  return data;
}

async function init() {
  const [health, catalog] = await Promise.all([api("/healthz"), api("/api/catalog")]);
  state.catalog = catalog;
  catalog.attributes.forEach((a) => (state.attrs[a.name] = a));
  const pill = $("model-pill");
  pill.textContent = health.provider === "anthropic" ? `AI: ${health.model}` : `AI: ${health.provider} mode`;
  pill.classList.toggle("live", health.provider === "anthropic");
  pill.title = `${health.governance_lint} · governance ${Object.entries(health.governance).map(([k, v]) => `${k} ${v}`).join(", ")}`;
  if (health.eval) {
    const sets = [health.eval.dev, health.eval.heldout, health.eval.refine].filter(Boolean);
    const cases = sets.reduce((n, e) => n + e.cases, 0);
    const runs = sets.reduce((n, e) => n + e.cases * e.runs, 0);
    const right = sets.reduce((n, e) => n + Math.round(e.pass_rate * e.cases * e.runs), 0);
    $("accuracy-hint").textContent = `Measured on our ${cases} test cases, ${runs} runs (also used to choose this model): fully right in ${right} of ${runs}. The assistant can misread a request, so always check the summary before approving.`;
  }

  catalog.examples.forEach((text) => {
    const chip = document.createElement("button");
    chip.className = "chip"; chip.textContent = text; chip.title = text;
    chip.onclick = () => { $("request").value = text; interpret(); };
    $("examples").appendChild(chip);
  });
  $("interpret-btn").onclick = interpret;
  $("request").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) interpret(); });
  (catalog.refine_examples || []).forEach((text) => {
    const chip = document.createElement("button");
    chip.className = "chip"; chip.textContent = text;
    chip.onclick = () => { $("refine-input").value = text; refine(); };
    $("refine-examples").appendChild(chip);
  });
  $("refine-btn").onclick = refine;
  $("refine-input").addEventListener("keydown", (e) => { if (e.key === "Enter") refine(); });
  $("add-condition").onclick = addCondition;
  $("channel").onchange = () => { state.definition.channel = $("channel").value; recheck(); };
  ["aud-name", "approver", "goal"].forEach((id) => $(id).addEventListener("input", updateApprove));
  $("approve-btn").onclick = approve;
  $("refresh-insights").onclick = loadInsights;
  $("campaign-type").onchange = loadEvidence;
  $("learning-type").onchange = () => loadLearning();
  $("advise-btn").onclick = advise;
  document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => showView(t.dataset.view)));
  document.querySelectorAll(".step-nav .back").forEach((b) => (b.onclick = () => goStep(Number(b.dataset.go))));
  document.querySelectorAll("#stepper li").forEach((li) => (li.onclick = () => {
    const n = Number(li.dataset.step);
    if (n <= maxStep()) goStep(n);
  }));
  $("to-size").onclick = () => goStep(3);
  $("to-plan").onclick = () => goStep(4);
  $("go-audiences").onclick = () => showView("audiences");
  $("new-audience").onclick = newAudience;
  goStep(1);
  loadAudit();
  loadInsights();
  loadHistoryTypes();
  loadLearning();
  const review = new URLSearchParams(location.search).get("review");
  if (review) loadForReview(review);
}

const SYNTHETIC = '<span class="synthetic-tag">synthetic history</span>';

async function loadHistoryTypes() {
  const h = await api("/api/history");
  if (!h.available) return;
  state.historySynthetic = h.synthetic;
  h.types.forEach((t) => {
    const o = document.createElement("option");
    o.value = t.id; o.textContent = `${t.label} (${t.channel.toUpperCase()}, ${t.campaigns} past)`;
    $("campaign-type").appendChild(o);
  });
  const other = document.createElement("option");
  other.value = "other"; other.textContent = "Other: nothing comparable in past campaigns";
  $("campaign-type").appendChild(other);
  $("campaign-type-row").hidden = false;
  loadAudit();
}

async function loadEvidence() {
  const type = $("campaign-type").value;
  const box = $("evidence");
  updateApprove();
  if (type === "other") {
    box.innerHTML = '<span class="hint-inline">No past campaigns to compare with. 10% is still held out, so this one will start the evidence for its kind.</span>';
    box.classList.remove("hidden");
    return;
  }
  if (!type) { box.classList.add("hidden"); return; }
  const e = await api(`/api/history/evidence/${encodeURIComponent(type)}`);
  const recent = e.per_campaign.slice(0, 4).map((c) =>
    `<li>${esc(c.name)}: ${c.lift_points >= 0 ? "+" : ""}${c.lift_points} pts (95%: ${c.lift_95_low} to ${c.lift_95_high}), ${fmt(c.contacted)} contacted, ${fmt(c.held_out)} held out</li>`).join("");
  box.innerHTML = `<strong>What happened last time</strong>${e.synthetic ? SYNTHETIC : ""}<p style="margin:6px 0 0">${esc(e.reading)}</p>`
    + `<p style="margin:6px 0 0"><strong>In plain terms:</strong> about ${perThousand(e.pooled.lift_points)} extra responders for every 1,000 messages sent.</p><ul>${recent}</ul>`;
  box.classList.remove("hidden");
  if (!state.definition) return;
  const p = await api("/api/measurement-preview", { definition: state.definition, campaign_type: type }).catch(() => null);
  if (p && p.available && p.detectable_lift_points != null) {
    box.innerHTML += `<p class="${p.power_note ? "warn-text" : "hint"}" style="margin:6px 0 0"><strong>Will this campaign's result be readable?</strong> `
      + (p.power_note ? esc(p.power_note)
        : `With about ${fmt(p.holdout_lines)} lines held out, a lift of ${p.detectable_lift_points.toFixed(2)} points or more would show clearly; past campaigns lifted ${signed(p.past_lift_points)}.`) + "</p>";
  }
  if (p && p.overlap_note) box.innerHTML += `<p class="warn-text" style="margin:6px 0 0"><strong>Overlap:</strong> ${esc(p.overlap_note)}</p>`;
}

function renderPressure(p) {
  const box = $("pressure");
  if (!p) { box.classList.add("hidden"); return; }
  const pct = (n) => (p.reachable ? Math.round((100 * n) / p.reachable) : 0) + "%";
  box.innerHTML = `<strong>Contact pressure</strong>${p.synthetic ? SYNTHETIC : ""}<br>`
    + `${pct(p.contacted_recently)} of these lines got a campaign in the last ${p.recent_days} days, ${pct(p.contacted_in_window)} in the last ${p.window_days}; `
    + `${fmt(p.at_or_over_cap)} had ${p.cap} or more in ${p.window_days} days. <span class="hint">(${esc(p.policy || "")}, as of ${esc(p.as_of)})</span>`;
  box.classList.remove("hidden");
}

const KIND_LABELS = { narrow_segment: "narrow the audience", exclude_segment: "exclude a segment",
  switch_channel: "change channel", choose_campaign_type: "campaign type", advice: "point to consider" };

async function advise() {
  const btn = $("advise-btn"), box = $("advice");
  btn.disabled = true; btn.innerHTML = '<span class="spinner dark"></span>Analysing…';
  try {
    const r = await api("/api/advise", { definition: state.definition, goal: $("goal").value,
      campaign_type: $("campaign-type").value || null, request_id: state.requestId });
    state.advice = r.proposals;
    state.adviceId = r.advice_id;
    const proposals = r.proposals.map((p, i) => {
      const action = { narrow_segment: "Apply", exclude_segment: "Apply", switch_channel: `Switch to ${(p.channel || "").toUpperCase()}`,
        choose_campaign_type: "Use this campaign type" }[p.kind];
      return `<div class="proposal"><div class="kind">${esc(KIND_LABELS[p.kind] || p.kind)}</div><strong>${esc(p.title)}</strong>`
        + `<p style="margin:4px 0">${esc(p.rationale)}</p>`
        + (p.consequence?.note ? `<div class="consequence-line">Computed: ${esc(p.consequence.note)}</div>` : "")
        + `<details><summary class="hint">Based on ${p.cited_facts.length} fact${p.cited_facts.length > 1 ? "s" : ""}</summary><ul class="cited">${p.cited_facts.map((f) => `<li>${esc(f)}</li>`).join("")}</ul></details>`
        + (action ? `<button class="small apply-advice" data-i="${i}">${esc(action)}</button>` : "") + "</div>";
    }).join("");
    const withheld = r.rejected.length ? `<div class="withheld">Withheld by the checks: ${r.rejected.map((x) => `“${esc(x.title)}” (${esc(x.reason)})`).join("; ")}</div>` : "";
    const t = r.telemetry;
    box.innerHTML = `<strong>Advisor</strong>${r.synthetic_history ? SYNTHETIC : ""}`
      + `<div class="hint">The AI's words, from the facts the system computed. Code checked its citations and numbers, not its wording; the sizes and responder estimates under each proposal are computed by code.</div>`
      + `${r.summary ? `<p style="margin:6px 0 0">${esc(r.summary)}</p>` : ""}${proposals}${withheld}`
      + `<details><summary class="hint">What the advisor saw: ${r.facts.length} facts computed by the system · ${esc(t.model || "")} · ${(t.llm_ms / 1000).toFixed(1)} s · $${t.cost_usd.toFixed(4)}</summary><ul class="cited">${r.facts.map((f) => `<li><span class="mono">${esc(f.id)}</span> ${esc(f.text)}</li>`).join("")}</ul></details>`;
    box.classList.remove("hidden");
    box.querySelectorAll(".apply-advice").forEach((b) => (b.onclick = () => applyAdvice(state.advice[Number(b.dataset.i)])));
  } catch (e) {
    box.innerHTML = `<span class="error-text">Advisor unavailable: ${esc(e.data?.detail || e.message)}</span>`;
    box.classList.remove("hidden");
  } finally {
    btn.disabled = false; btn.textContent = "Ask the advisor";
  }
}

async function applyAdvice(p) {
  if (p.kind === "choose_campaign_type") {
    $("campaign-type").value = p.campaign_type;
    await loadEvidence();
  } else if (p.consequence?.definition) {
    state.definition = p.consequence.definition;
    state.appliedAdviceId = state.adviceId;
    await recheck();
  }
  $("advice").classList.add("hidden");
}

async function loadForReview(requestId) {
  try {
    const r = await api(`/api/interpretations/${encodeURIComponent(requestId)}`);
    state.requestId = r.request_id;
    state.appliedAdviceId = null;
    state.unmapped = r.unmapped_phrases;
    state.acknowledged = false;
    $("request").value = r.request_text || "";
    applyResult(r);
    renderChanges(null);
    renderTelemetry(r.telemetry, r.request_id);
    const who = r.telemetry.origin === "mcp" ? "drafted by an AI agent (MCP)" : "recorded earlier";
    $("review-note").textContent = `Reviewing request ${r.request_id}, ${who}. Check it against today's rules, answer any question, then size and approve.`;
    $("review-note").hidden = false;
    goStep(2);
  } catch (e) {
    $("interpret-error").textContent = `Couldn't load request ${requestId} for review: ${e.data?.detail || e.message}`;
    $("interpret-error").hidden = false;
  }
}

async function interpret() {
  const text = $("request").value.trim();
  if (!text) return;
  const btn = $("interpret-btn");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Interpreting…';
  $("interpret-error").hidden = true;
  $("refine-error").hidden = true;
  $("review-note").hidden = true;
  try {
    const r = await api("/api/interpret", { request: text });
    state.requestId = r.request_id;
    state.appliedAdviceId = null;
    state.unmapped = r.unmapped_phrases;
    state.acknowledged = false;
    state.pendingTerms = r.clarifications.map((c) => (c.either ? `any:${c.term}` : c.negated ? `not:${c.term}` : c.term));
    applyResult(r);
    renderChanges(null);
    renderTelemetry(r.telemetry, r.request_id);
    renderAssumptions(r.assumptions);
    $("approve-msg").textContent = "";
    goStep(2);
  } catch (e) {
    const detail = typeof e.data?.detail === "string" ? e.data.detail : e.message;
    $("interpret-error").textContent = `Request failed: ${detail}${e.data?.request_id ? ` (request ${e.data.request_id})` : ""}`;
    $("interpret-error").hidden = false;
  } finally {
    btn.disabled = false; btn.textContent = "Interpret";
  }
}

async function refine() {
  const text = $("refine-input").value.trim();
  if (!text || !state.requestId) return;
  const btn = $("refine-btn");
  btn.disabled = true; btn.innerHTML = '<span class="spinner dark"></span>Applying…';
  $("refine-error").hidden = true;
  try {
    const r = await api("/api/refine", { request_id: state.requestId, instruction: text, definition: state.definition });
    const before = new Set(state.unmapped);
    state.requestId = r.request_id;
    state.appliedAdviceId = null;
    state.unmapped = r.unmapped_phrases;
    state.acknowledged = state.acknowledged && r.unmapped_phrases.every((p) => before.has(p));
    applyResult(r);
    renderChanges(r.changes);
    renderTelemetry(r.telemetry, r.request_id);
    renderAssumptions(r.assumptions);
    $("refine-input").value = "";
    $("approve-msg").textContent = "";
  } catch (e) {
    const detail = typeof e.data?.detail === "string" ? e.data.detail : e.message;
    $("refine-error").textContent = `Change not applied: ${detail}`;
    $("refine-error").hidden = false;
  } finally {
    btn.disabled = !state.requestId; btn.textContent = "Apply change";
  }
}

function renderChanges(changes) {
  const box = $("changes");
  const lines = changes ? [
    ...changes.added.map((t) => `<li class="added">+ ${esc(t)}</li>`),
    ...changes.removed.map((t) => `<li class="removed">− <s>${esc(t)}</s></li>`),
    ...(changes.channel ? [`<li>Channel: ${esc(changes.channel)}</li>`] : []),
  ] : [];
  box.classList.toggle("hidden", !changes);
  box.innerHTML = lines.length ? `<strong>What changed</strong><ul>${lines.join("")}</ul>` : "<strong>Nothing changed.</strong>";
}

function applyResult(r) {
  state.status = r.status;
  state.definition = r.definition;
  state.pendingTerms = r.clarifications.map((c) => (c.either ? `any:${c.term}` : c.negated ? `not:${c.term}` : c.term));
  $("refine-btn").disabled = !state.requestId;
  $("channel").value = r.definition.channel;
  renderBanner();
  renderClarifications(r.clarifications);
  renderUnmapped();
  $("summary").textContent = r.explanation.summary;
  $("summary-label").textContent = displayStatus() === "ready"
    ? "This audience is exactly:" : "Current definition (not final until the points above are resolved):";
  state.errorFields = new Set(r.issues.filter((i) => i.level === "error" && i.attribute).map((i) => i.attribute));
  renderConditions();
  renderIssues($("issues"), r.issues);
  const sizable = ["ready", "needs_confirmation"].includes(displayStatus());
  if (sizable && state.definition.conditions.length) size(); else hideSize();
  updateNav();
}

function displayStatus() {
  if (["ready", "needs_confirmation"].includes(state.status) && state.unmapped.length) {
    return state.acknowledged ? "ready" : "needs_confirmation";
  }
  return state.status;
}

function renderBanner() {
  const s = displayStatus();
  const el = $("status-banner");
  el.className = `banner ${s}`;
  el.textContent = STATUS_TEXT[s] || s;
}

function renderClarifications(clarifications) {
  const box = $("clarify");
  box.innerHTML = "";
  clarifications.forEach((c) => {
    const div = document.createElement("div");
    div.className = "clarify-box";
    div.innerHTML = `<strong>${esc(c.question)}</strong>`;
    c.options.forEach((o) => {
      const b = document.createElement("button");
      b.className = "option";
      const consequence = o.reachable === null || o.reachable === undefined ? ""
        : `<span class="consequence">${fmt(o.reachable)} reachable · ${fmt(o.only_here)} of them only with this choice</span>`;
      b.innerHTML = `${esc(o.label)}${o.working_default ? ' <span class="badge default">working default</span>' : ""}${consequence}`;
      b.onclick = () => resolve(c.term, o.id);
      div.appendChild(b);
    });
    if (c.working_default_note) {
      const note = document.createElement("p");
      note.className = "hint"; note.textContent = c.working_default_note;
      div.appendChild(note);
    }
    box.appendChild(div);
  });
}

function renderUnmapped() {
  const box = $("unmapped");
  box.innerHTML = "";
  if (!state.unmapped.length) return;
  const div = document.createElement("div");
  div.className = "unmapped-box";
  div.innerHTML = `No data available for: ${state.unmapped.map((p) => `<q>${esc(p)}</q>`).join(", ")}.
    <label class="inline" style="margin-top:8px"><input type="checkbox" id="ack" ${state.acknowledged ? "checked" : ""}>
    Build the audience without this part</label>`;
  box.appendChild(div);
  $("ack").onchange = (e) => { state.acknowledged = e.target.checked; renderBanner(); updateApprove(); updateNav(); if (state.acknowledged) size(); };
}

function sourceBadge(source) {
  if (source === "model") return '<span class="badge model">AI</span>';
  if (source === "user") return '<span class="badge user">edited</span>';
  if (source === "advisor") return '<span class="badge advisor">advisor</span>';
  const ref = source.replace("glossary:", "");
  if (ref.includes("=")) return `<span class="badge choice" title="${esc(ref)}">your choice</span>`;
  const term = state.catalog.terms[ref];
  const params = Object.entries(term?.parameters || {}).map(([n, v]) => `${n} v${v}`).join(", ");
  return `<span class="badge governed" title="${esc((term?.rule || "") + (params ? ` (parameters: ${params})` : ""))}">governed · ${esc(term?.decision_id || ref)}</span>`;
}

function renderConditions() {
  const tbody = $("conditions");
  tbody.innerHTML = "";
  state.definition.conditions.forEach((cond, i) => {
    const attr = state.attrs[cond.attribute];
    const tr = document.createElement("tr");
    if (state.errorFields?.has(cond.attribute)) tr.className = "row-error";
    const attrOptions = state.catalog.attributes
      .map((a) => `<option value="${a.name}" ${a.name === cond.attribute ? "selected" : ""}>${esc(a.label)}</option>`).join("");
    const ops = attr ? OPS[attr.type] : [cond.op];
    const opOptions = ops.map((op) => `<option value="${op}" ${op === cond.op ? "selected" : ""}>${OP_LABELS[op]}</option>`).join("");
    tr.innerHTML = `
      <td><select data-k="attribute">${attr ? "" : `<option selected>${esc(cond.attribute)}</option>`}${attrOptions}</select></td>
      <td><select data-k="op">${opOptions}</select></td>
      <td>${attr ? valueWidget(attr, cond) : esc(JSON.stringify(cond.value))}</td>
      <td>${sourceBadge(cond.source)}</td>
      <td><button class="remove" title="Remove">✕</button></td>`;
    tr.querySelector('[data-k="attribute"]').onchange = (e) => editCondition(i, { attribute: e.target.value });
    tr.querySelector('[data-k="op"]').onchange = (e) => editCondition(i, { op: e.target.value });
    tr.querySelectorAll("[data-v]").forEach((el) => (el.onchange = () => editCondition(i, { value: readValue(tr, attr, cond.op) })));
    tr.querySelector(".remove").onclick = () => { state.definition.conditions.splice(i, 1); recheck(); };
    tbody.appendChild(tr);
  });
}

function valueWidget(attr, cond) {
  if (attr.type === "boolean") {
    return `<select data-v><option value="true" ${cond.value ? "selected" : ""}>yes</option><option value="false" ${cond.value ? "" : "selected"}>no</option></select>`;
  }
  if (attr.type === "enum") {
    const multi = ["in", "not_in"].includes(cond.op);
    const selected = new Set(multi ? cond.value : [cond.value]);
    const opts = attr.values.map((v) => `<option ${selected.has(v) ? "selected" : ""}>${esc(v)}</option>`).join("");
    return `<select data-v ${multi ? "multiple" : ""}>${opts}</select>`;
  }
  const unit = attr.unit ? `<span class="unit">${esc(attr.unit)}</span>` : "";
  if (cond.op === "between") {
    const [lo, hi] = Array.isArray(cond.value) ? cond.value : [attr.min, attr.max];
    return `<div class="between-inputs value-cell"><input data-v type="number" value="${esc(lo)}"><input data-v type="number" value="${esc(hi)}">${unit}</div>`;
  }
  return `<div class="value-cell"><input data-v type="number" value="${esc(cond.value)}">${unit}</div>`;
}

function readValue(tr, attr, op) {
  const els = [...tr.querySelectorAll("[data-v]")];
  if (attr.type === "boolean") return els[0].value === "true";
  if (attr.type === "enum") {
    const chosen = [...els[0].selectedOptions].map((o) => o.value);
    return ["in", "not_in"].includes(op) ? chosen : chosen[0];
  }
  if (op === "between") return els.map((el) => Number(el.value));
  return Number(els[0].value);
}

function defaultValue(attr, op) {
  if (attr.type === "boolean") return true;
  if (attr.type === "enum") return ["in", "not_in"].includes(op) ? [attr.values[0]] : attr.values[0];
  if (op === "between") return [attr.min, attr.max];
  return attr.min;
}

function editCondition(i, change) {
  const cond = { ...state.definition.conditions[i], ...change, source: "user" };
  const attr = state.attrs[cond.attribute];
  if (change.attribute) { cond.op = OPS[attr.type][0]; cond.value = defaultValue(attr, cond.op); }
  if (change.op) {
    const wasList = Array.isArray(state.definition.conditions[i].value);
    const needsList = ["in", "not_in", "between"].includes(cond.op);
    if (wasList !== needsList) cond.value = defaultValue(attr, cond.op);
  }
  state.definition.conditions[i] = cond;
  recheck();
}

function addCondition() {
  const used = new Set(state.definition.conditions.map((c) => c.attribute));
  const attr = state.catalog.attributes.find((a) => !used.has(a.name)) || state.catalog.attributes[0];
  state.definition.conditions.push({ attribute: attr.name, op: OPS[attr.type][0], value: defaultValue(attr, "eq"), source: "user" });
  recheck();
}

async function resolve(term, optionId) {
  await recheck({ [term]: optionId });
}

async function recheck(resolutions = {}) {
  const r = await api("/api/check", { definition: state.definition, pending_terms: state.pendingTerms, resolutions,
    advice_id: state.appliedAdviceId || null, request_id: state.requestId });
  applyResult({ ...r, unmapped_phrases: state.unmapped });
  renderChanges(null);
}

async function size() {
  try {
    const s = await api("/api/size", state.definition);
    state.sized = s;
    $("reachable").textContent = fmt(s.reachable);
    $("matched").textContent = fmt(s.matched);
    $("excluded").textContent = fmt(s.excluded_no_consent);
    $("share").textContent = (s.share_of_base * 100).toFixed(1) + "%";
    renderBars($("by-region"), s.by_region, s.reachable);
    renderBars($("by-line-type"), s.by_line_type, s.reachable);
    renderPressure(s.contact_pressure);
    renderIssues($("size-warnings"), [...s.warnings, ...(s.contact_pressure?.warnings || [])]);
    $("size-meta").textContent = `Counted over ${fmt(s.base)} lines in ${s.query_ms} ms · one row per line, consent applied for ${state.definition.channel.toUpperCase()}.`;
  } catch (e) {
    hideSize();
  }
  updateApprove();
  updateNav();
}

function hideSize() {
  state.sized = null;
  if (state.step === 3 || state.step === 4) goStep(2);  // the audience changed and can't be sized: back to review
  updateNav();
}

// ---- The journey: one step at a time, each unlocked when the one before is resolved ----

function canContinueReview() {
  return Boolean(state.sized) && displayStatus() === "ready" && !state.pendingTerms.length
    && state.definition.conditions.length > 0;
}

function maxStep() {
  if (!state.requestId) return 1;
  return canContinueReview() ? 4 : 2;
}

function goStep(n) {
  state.step = n;
  document.querySelectorAll(".step-panel").forEach((p) => p.classList.toggle("hidden", Number(p.dataset.step) !== n));
  $("stepper").classList.toggle("hidden", n === 5);
  updateNav();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function updateNav() {
  const top = maxStep();
  document.querySelectorAll("#stepper li").forEach((li) => {
    const n = Number(li.dataset.step);
    li.className = n === state.step ? "current" : n < state.step || n <= top ? "done" : "locked";
  });
  const ok = canContinueReview();
  $("to-size").disabled = !ok;
  $("review-hint").textContent = ok ? "" : (REVIEW_HINTS[displayStatus()] || (state.sized ? "" : "Sizing…"));
}

function showView(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  ["build", "audiences", "insights"].forEach((v) => $(`view-${v}`).classList.toggle("hidden", v !== name));
  if (name === "audiences") { loadAudit(); loadLearning(); }
  if (name === "insights") loadInsights();
  window.scrollTo({ top: 0 });
}

function newAudience() {
  Object.assign(state, { requestId: null, status: null, definition: { conditions: [], channel: "sms" }, pendingTerms: [],
    unmapped: [], acknowledged: false, sized: null, appliedAdviceId: null, advice: null, adviceId: null });
  ["request", "refine-input", "goal", "aud-name"].forEach((id) => ($(id).value = ""));
  $("campaign-type").value = "";
  ["evidence", "advice", "changes"].forEach((id) => $(id).classList.add("hidden"));
  ["review-note", "interpret-error", "refine-error"].forEach((id) => ($(id).hidden = true));
  $("approve-msg").textContent = "";
  updateApprove();
  showView("build");
  goStep(1);
  $("request").focus();
}

function renderBars(el, counts, total) {
  el.innerHTML = Object.entries(counts).map(([k, n]) => `
    <div class="bar"><span>${esc(k)}</span><div class="track"><div class="fill" style="width:${total ? (100 * n / total).toFixed(1) : 0}%"></div></div><span class="n">${fmt(n)}</span></div>`).join("")
    || '<p class="hint">No reachable lines.</p>';
}

function renderIssues(el, issues) {
  el.innerHTML = issues.map((i) => `<li class="${i.level}">${esc(i.message)}</li>`).join("");
}

function renderAssumptions(list) {
  $("assumptions-box").classList.toggle("hidden", !list.length);
  $("assumptions").innerHTML = list.map((a) => `<li>${esc(a)}</li>`).join("");
}

function renderTelemetry(t, requestId) {
  const parts = [`request ${requestId}`, ...(t.origin === "mcp" ? ["via MCP"] : []), `model ${t.model || "n/a"}`,
    `${(t.total_ms / 1000).toFixed(2)} s`];
  if (t.llm_calls) parts.push(`${t.llm_calls} AI call${t.llm_calls > 1 ? "s" : ""}`, `${fmt(t.input_tokens + t.cache_read_tokens + t.cache_write_tokens)} in / ${fmt(t.output_tokens)} out tokens`, `$${t.cost_usd.toFixed(4)}`);
  if (t.cache_hit) parts.push("cached answer");
  if (t.repaired) parts.push("self-corrected once");
  $("telemetry").textContent = parts.join(" · ");
  $("pii-note").textContent = t.pii_masked
    ? `${t.pii_masked} personal detail${t.pii_masked > 1 ? "s were" : " was"} removed from your request before the AI saw it.` : "";
}

function updateApprove() {
  const typeChosen = $("campaign-type-row").hidden || $("campaign-type").value !== "";
  const missing = [
    $("goal").value.trim().length < 3 && "what the campaign is for",
    !typeChosen && "a campaign type (or Other)",
    $("aud-name").value.trim().length < 3 && "an audience name",
    $("approver").value.trim().length < 2 && "your name",
  ].filter(Boolean);
  const ok = state.sized && displayStatus() === "ready" && !state.pendingTerms.length && !missing.length;
  $("approve-btn").disabled = !ok;
  $("approve-hint").textContent = missing.length ? `Add ${missing.join(", ")} to approve.` : "";
}

async function approve() {
  try {
    const r = await api("/api/approve", {
      request_id: state.requestId, name: $("aud-name").value, approver: $("approver").value, goal: $("goal").value,
      campaign_type: $("campaign-type").value || null,
      definition: state.definition, acknowledged_unmapped: state.acknowledged,
      advice_id: state.appliedAdviceId || null,
    });
    const m = r.measurement;
    $("approve-msg").textContent = "";
    $("done-title").textContent = `Audience #${r.id} approved: ${$("aud-name").value.trim()}`;
    $("done-detail").innerHTML =
      `<p><strong>${fmt(m.target_lines)}</strong> lines get the campaign and <strong>${fmt(m.holdout_lines)}</strong> `
      + `(${Math.round(m.holdout_share * 100)}%) are held out, so its effect can be measured.</p>`
      + `<p><strong>How we'll know:</strong> ${esc(m.success_measure)}</p>`
      + (m.measurable ? "" : `<p class="warn-text">${esc(m.note)}</p>`)
      + (m.power_note ? `<p class="warn-text">${esc(m.power_note)}</p>` : "")
      + (m.overlap_note ? `<p class="warn-text">${esc(m.overlap_note)}</p>` : "")
      + `<p class="hint">Next: export the lines to contact from Audiences &amp; results, send the campaign, and record what happened after ${m.window_days} days.</p>`;
    goStep(5);
    loadAudit();
    loadLearning();
  } catch (e) {
    $("approve-msg").textContent = "Not approved: " + approvalError(e.data?.detail, e.message);
  }
}

function approvalError(d, fallback) {
  // The server's reasons, in words: a marketer shouldn't have to read JSON.
  if (typeof d === "string") return d;
  if (!d) return fallback;
  if (d.unanswered_terms) return `answer the question about ${d.unanswered_terms.map((t) => termLabel(t.replace(/^(not|any):/, ""))).join(", ")} first.`;
  if (d.unmapped_phrases) return `confirm building without: ${d.unmapped_phrases.join(", ")}.`;
  if (d.issues?.length) return d.issues.map((i) => i.message).join(" ");
  return d.detail || fallback;
}

async function loadAudit() {
  const rows = await api("/api/audiences");
  $("count-audiences").textContent = rows.length ? rows.length : "";
  $("audit").innerHTML = rows.map((r) => {
    const m = JSON.parse(r.measurement_json || "{}");
    const typeLabel = m.campaign_type ? ($("campaign-type").querySelector(`option[value="${m.campaign_type}"]`)?.textContent || m.campaign_type).replace(/ \(.*/, "") : null;
    const notes = [
      r.data_changed_since_approval ? '<span class="hint-inline">Profile data refreshed since approval; the export is still the approved list.</span>' : "",
      r.excluded_since_approval ? `<span class="warn-text">${fmt(r.excluded_since_approval)} lines left out before the hand-off (consent withdrawn or no longer in the data).</span>` : "",
      r.handed_off_at ? `<span class="hint-inline">Handed off on ${esc(r.handed_off_at.slice(0, 10))}: the lists are frozen from then on.</span>` : "",
    ].filter(Boolean).join(" ");
    return `<article class="aud-card">
      <div class="aud-head">
        <span class="aud-id">#${r.id}</span><strong class="aud-name">${esc(r.name)}</strong>
        <span class="aud-gov">${governanceCell(r)}</span>
      </div>
      <div class="aud-meta">${fmt(r.reachable)} reachable · approved by ${esc(r.approver)} · ${esc(r.created_at.slice(0, 10))}${r.edited_by_user ? " · edited before approval" : ""}</div>
      <p class="aud-def">${esc(r.explanation)}</p>
      ${m.goal ? `<div class="aud-plan"><span><strong>Goal:</strong> ${esc(m.goal)}</span>${typeLabel ? `<span><strong>Type:</strong> ${esc(typeLabel)}</span>` : ""}<span><strong>Held out:</strong> ${fmt(m.holdout_lines)}${m.measurable ? "" : " (too few to be reliable)"}</span></div>` : ""}
      <div class="aud-actions">Export for the campaign tool:
        <a href="/api/audiences/${r.id}/export.csv">lines to contact</a>
        <a href="/api/audiences/${r.id}/export.csv?group=held_out">holdout (don't send)</a></div>
      ${notes ? `<div class="aud-notes">${notes}</div>` : ""}
      ${m.goal ? outcomeLine(r, m) : ""}
      <details class="tech"><summary>Technical</summary><span class="mono">definition ${r.definition_hash} · ${esc(r.model || "manual")} · ${esc(r.created_at.replace("T", " ").replace("+00:00", " UTC"))}</span></details>
    </article>`;
  }).join("") || '<p class="empty">Nothing approved yet. Build one under <a href="#" id="empty-new">New audience</a>.</p>';
  $("empty-new")?.addEventListener("click", (e) => { e.preventDefault(); showView("build"); });
  document.querySelectorAll(".simulate-btn").forEach((b) => (b.onclick = () => simulateOutcome(b.dataset.id, b)));
  document.querySelectorAll(".confirm-btn").forEach((b) => (b.onclick = () => confirmOutcome(b.dataset.id, b)));
}

function verdictClass(v) {
  if (v.startsWith("in line")) return "verdict-ok";
  if (v.startsWith("too small") || v.startsWith("no expectation")) return "verdict-neutral";
  return "verdict-diff";
}

const signed = (v) => `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}`;
const perThousand = (points) => Math.round(Number(points) * 10).toLocaleString("en-US");
const termLabel = (name) => name.charAt(0).toUpperCase() + name.slice(1).replace(/_/g, " ");

function outcomeLine(r, m) {
  if (r.outcome) {
    const o = r.outcome.measured;
    const pending = r.outcome.status === "pending";
    const tone = pending ? "pending" : r.outcome.verdict.startsWith("too small") ? "" : "measured";
    return `<div class="aud-outcome ${tone}"><strong>${pending ? "Reported" : "Measured"}</strong> (${esc(r.outcome.source === "simulated" ? "simulated send" : "campaign tool")}): `
      + `about ${perThousand(o.lift_points)} extra responders per 1,000 messages, ${signed(o.lift_points)} pts (95% ${signed(o.lift_95_low)} to ${signed(o.lift_95_high)})`
      + `${r.outcome.expected ? ` against ${signed(r.outcome.expected.lift_points)} expected` : ""}: <span class="${verdictClass(r.outcome.verdict)}">${esc(r.outcome.verdict)}</span>`
      + (pending ? `<br>Not learned from until someone checks it: ${fmt(o.contacted)} contacted, ${fmt(o.held_out)} held out, ${fmt(r.outcome.unknown_responders)} responders not in this audience. `
        + (r.outcome.check_before_confirming ? `<span class="warn-text">This is far from what past campaigns showed: it may be real, or the wrong file. Check it before confirming.</span> ` : "")
        + `<button class="small confirm-btn" data-id="${r.id}">Confirm outcome</button>`
        : ` · confirmed by ${esc(r.outcome.confirmed_by)}`)
      + (r.outcome.kept_out_of_evidence ? `<br><span class="hint-inline">Not added to the evidence: this audience overlapped a recent approval, so it isn't an independent campaign.</span>` : "")
      + "</div>";
  }
  if (!m.campaign_type) return "";
  return `<div class="aud-outcome">Not measured yet. After the ${m.window_days}-day window, the data team reports who responded (from billing records), and the result waits here for someone to confirm it. `
    + (state.historySynthetic ? `<button class="small simulate-btn" data-id="${r.id}">Simulate the send (demo)</button>` : "") + "</div>";
}

async function simulateOutcome(id, btn) {
  btn.disabled = true; btn.textContent = "Measuring…";
  try {
    await api(`/api/audiences/${id}/simulate-outcome`, {});
  } catch (e) {
    btn.textContent = `Failed: ${e.data?.detail || e.message}`;
    return;
  }
  await Promise.all([loadAudit(), loadLearning()]);
  if ($("campaign-type").value) loadEvidence();
}

async function confirmOutcome(id, btn) {
  const by = window.prompt("Your name, to confirm you checked these numbers (not the audience's approver):", "");
  if (!by || !by.trim()) return;
  btn.disabled = true;
  try {
    await api(`/api/audiences/${id}/outcome/confirm`, { confirmed_by: by.trim() });
  } catch (e) {
    btn.textContent = `Failed: ${e.data?.detail || e.message}`;
    return;
  }
  await Promise.all([loadAudit(), loadLearning()]);
  if ($("campaign-type").value) loadEvidence();
}

async function loadLearning() {
  const s = await api("/api/learning");
  if (!s.types.length) { $("learning-card").hidden = true; return; }
  $("learning-synthetic").innerHTML = s.synthetic ? SYNTHETIC : "";
  const sel = $("learning-type");
  if (!sel.options.length) {
    s.types.forEach((t) => { const o = document.createElement("option"); o.value = t.id; o.textContent = t.label; sel.appendChild(o); });
    const measured = s.types.find((t) => t.measured_here);
    sel.value = measured ? measured.id : "data_upsell";
  }
  const t = s.types.find((x) => x.id === sel.value) || s.types[0];
  const width = (e) => (e.lift_95_high - e.lift_95_low) / 2;
  const L = s.loop;
  $("loop").innerHTML = [
    `<div class="stage"><strong>Define</strong><span class="n">${fmt(L.interpreted)}</span> ${L.interpreted === 1 ? "request" : "requests"} turned into governed definitions</div>`,
    `<div class="stage"><strong>Decide</strong><span class="n">${fmt(L.approved)}</span> approved, each with a goal and a holdout (${fmt(L.approved_with_type)} with a campaign type)</div>`,
    `<div class="stage"><strong>Send and measure</strong><span class="n">${fmt(L.measured)}</span> ${L.measured === 1 ? "outcome" : "outcomes"} measured against ${L.measured === 1 ? "its" : "their"} holdout and confirmed${L.pending_confirmation ? ` (${fmt(L.pending_confirmation)} waiting for a check)` : ""}</div>`,
    `<div class="stage learn"><strong>Learn</strong>${esc(t.label)}: <span class="n">${signed(t.estimate_now.lift_points)}</span> pts ± ${width(t.estimate_now).toFixed(2)} from ${t.campaigns} campaigns${t.measured_here ? `, ${t.measured_here} of them approved here (history alone: ${signed(t.estimate_from_history_only.lift_points)} ± ${width(t.estimate_from_history_only).toFixed(2)})` : ""}</div>`,
    `<div class="stage"><strong>Next decision</strong>the next ${esc(t.label.toLowerCase())} approval sees this evidence</div>`,
  ].join('<span class="arrow">→</span>');
  $("learning-reading").textContent = `Each dot is one campaign's lift against its holdout; the line is the estimate after it.`;
  $("scorecard").innerHTML = s.scorecard.map((c) => `<tr><td>${c.audience_id}</td><td>${esc(c.name)}</td><td>${esc((s.types.find((x) => x.id === c.campaign_type) || {}).label || c.campaign_type)}</td>`
    + `<td>${c.expected ? `${signed(c.expected.lift_points)} pts (95% ${signed(c.expected.lift_95_low)} to ${signed(c.expected.lift_95_high)}), ${c.expected.campaigns} campaigns` : "none recorded"}</td>`
    + `<td>${signed(c.measured.lift_points)} pts (95% ${signed(c.measured.lift_95_low)} to ${signed(c.measured.lift_95_high)})<div class="hint">${fmt(c.measured.contacted)} contacted, ${fmt(c.measured.held_out)} held out · ${esc(c.source === "simulated" ? "simulated send" : "campaign tool")}</div></td>`
    + `<td class="${verdictClass(c.verdict)}">${esc(c.verdict)}${c.status === "pending" ? '<div class="hint">pending: not learned from until confirmed</div>' : ""}${c.kept_out_of_evidence ? '<div class="hint">not added to the evidence: overlapped a recent approval</div>' : ""}</td></tr>`).join("")
    || '<tr><td colspan="6" class="hint">No outcomes yet. Approve an audience with a campaign type, then measure its send.</td></tr>';
  drawCurve((await api(`/api/learning/${encodeURIComponent(sel.value)}`)).timeline);
}

function drawCurve(points) {
  const W = 900, H = 300, L = 62, R = 96, T = 16, B = 40;
  const values = points.flatMap((p) => [p.lift_95_low, p.lift_95_high, p.estimate_low, p.estimate_high]).concat([0]);
  let lo = Math.min(...values), hi = Math.max(...values);
  const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
  const x = (i) => L + (W - L - R) * (points.length === 1 ? 0.5 : i / (points.length - 1));
  const y = (v) => T + (H - T - B) * (hi - v) / (hi - lo);
  const band = points.map((p, i) => `${x(i)},${y(p.estimate_high)}`)
    .concat(points.map((p, i) => [i, p]).reverse().map(([i, p]) => `${x(i)},${y(p.estimate_low)}`)).join(" ");
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.estimate_after)}`).join(" ");
  const step = (hi - lo) > 4 ? 1 : 0.5;
  let ticks = "";
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    ticks += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#eef0f3"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="#5d6677">${signed(v).replace(".00", "")}</text>`;
  }
  const dots = points.map((p, i) => {
    const here = p.source !== "synthetic history";
    const c = here ? "#c2410c" : "#9aa3b2";
    return `<line x1="${x(i)}" x2="${x(i)}" y1="${y(p.lift_95_low)}" y2="${y(p.lift_95_high)}" stroke="${c}" stroke-width="${here ? 2.5 : 1.5}"/>`
      + `<circle cx="${x(i)}" cy="${y(p.lift_points)}" r="${here ? 6 : 4}" fill="${c}"><title>${esc(p.name)}: ${signed(p.lift_points)} pts (95% ${signed(p.lift_95_low)} to ${signed(p.lift_95_high)}); estimate after: ${signed(p.estimate_after)}</title></circle>`
      + (here ? `<text x="${x(i) + 9}" y="${y(p.lift_points) + 4}" text-anchor="start" font-size="11" fill="#c2410c">#${p.audience_id}</text>` : "");
  }).join("");
  const first = points[0], last = points[points.length - 1];
  $("learning-chart").innerHTML = ticks
    + `<line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" stroke="#5d6677" stroke-dasharray="4 4"/>`
    + `<text x="${L + 6}" y="${y(0) - 5}" font-size="11" fill="#5d6677">no effect</text>`
    + `<polygon points="${band}" fill="#e3f1f0" opacity="0.9"/>`
    + `<path d="${line}" fill="none" stroke="#0f6e6e" stroke-width="2.5"/>`
    + dots
    + `<text x="${L}" y="${H - 12}" font-size="11" fill="#5d6677">${esc(first.sent_at)}</text>`
    + `<text x="${W - R}" y="${H - 12}" text-anchor="end" font-size="11" fill="#5d6677">${esc(last.sent_at)}</text>`
    + `<text x="14" y="${T + (H - T - B) / 2}" font-size="11" fill="#5d6677" transform="rotate(-90 14 ${T + (H - T - B) / 2})" text-anchor="middle">lift vs holdout (points)</text>`;
}

function governanceCell(r) {
  if (!r.governance_changes.length) {
    const drift = r.reachable_now - r.reachable;
    return `<span class="gov-ok">current</span>${drift ? `<div class="hint">size now ${fmt(r.reachable_now)}</div>` : ""}`;
  }
  const delta = r.reachable_under_current_rules - r.reachable;
  const pct = r.reachable ? ((100 * delta) / r.reachable).toFixed(1) : "0";
  return `<span class="gov-stale">⚠ rules changed since approval</span>
    <div class="hint">${r.governance_changes.map(esc).join("<br>")}</div>
    <div class="hint">${r.reachable_under_current_rules === null ? "" : `under current rules: ${fmt(r.reachable_under_current_rules)} (${delta >= 0 ? "+" : ""}${pct}%). `}Needs re-approval before use.</div>`;
}

async function loadInsights() {
  const ins = await api("/api/insights");
  $("ins-demand").innerHTML = ins.unmapped_demand.map((d) =>
    `<li title="e.g. ${esc(d.example)}">${esc(d.phrase)}<span class="n">${d.count}×</span></li>`).join("")
    || '<li class="hint">Nothing yet.</li>';
  $("ins-terms").innerHTML = ins.open_terms.map((t) => {
    const chosen = Object.entries(t.chosen).filter(([, n]) => n).map(([label, n]) => `${esc(label)}: ${n}`).join("; ");
    return `<li><strong>${esc(termLabel(t.term))}</strong><span class="n">asked ${t.asked}×</span><div class="hint">${chosen || "no approvals yet"} · decides: ${esc(t.owner_pending)}</div></li>`;
  }).join("");
  $("ins-blocked").innerHTML = ins.blocked_by_policy.map((b) =>
    `<li>${esc(b.label)} <span class="hint">(${esc(b.policy)})</span><span class="n">${b.count}×</span></li>`).join("")
    || '<li class="hint">Nothing yet.</li>';
  const e = ins.edits;
  $("ins-edits").innerHTML = `<li>Approved audiences edited before approval<span class="n">${e.edited}/${e.approved}</span></li>`
    + `<li>Changes asked for in plain language<span class="n">${ins.refinements}</span></li>`
    + e.by_field.map((f) => `<li>${esc(f.label)}<span class="n">${f.count}×</span></li>`).join("");
}

init();

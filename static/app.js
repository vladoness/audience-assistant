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
  ready: "Ready. Check the summary, then size and approve.",
  needs_clarification: "One term in your request has competing definitions. Choose the one you mean.",
  needs_confirmation: "Part of your request can't be built from the available data. Confirm before approving.",
  unsupported: "This doesn't describe an audience the profile store can build. Try rephrasing, or use the editor.",
  invalid: "The proposal broke a data or policy rule. Fix the highlighted conditions below.",
  ai_unavailable: "The assistant is unavailable right now. You can still build the audience with the editor.",
};

const state = {
  catalog: null, attrs: {}, requestId: null, status: null, definition: { conditions: [], channel: "sms" },
  pendingTerms: [], unmapped: [], acknowledged: false, sized: null,
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
    const part = (e, label) => e ? `${Math.round(e.pass_rate * 100)}% on ${e.cases} ${label} cases` : null;
    const measured = [part(health.eval.heldout, "held-out"), part(health.eval.dev, "development")].filter(Boolean).join(", ");
    $("accuracy-hint").textContent = `Measured accuracy of this configuration: ${measured}. The assistant can misread a request, so always check the summary before approving.`;
  }

  catalog.examples.forEach((text) => {
    const chip = document.createElement("button");
    chip.className = "chip"; chip.textContent = text; chip.title = text;
    chip.onclick = () => { $("request").value = text; interpret(); };
    $("examples").appendChild(chip);
  });
  $("interpret-btn").onclick = interpret;
  $("request").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) interpret(); });
  $("add-condition").onclick = addCondition;
  $("channel").onchange = () => { state.definition.channel = $("channel").value; recheck(); };
  ["aud-name", "approver"].forEach((id) => $(id).addEventListener("input", updateApprove));
  $("approve-btn").onclick = approve;
  $("refresh-insights").onclick = loadInsights;
  loadAudit();
  loadInsights();
}

async function interpret() {
  const text = $("request").value.trim();
  if (!text) return;
  const btn = $("interpret-btn");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Interpreting…';
  $("interpret-error").hidden = true;
  try {
    const r = await api("/api/interpret", { request: text });
    state.requestId = r.request_id;
    state.unmapped = r.unmapped_phrases;
    state.acknowledged = false;
    state.pendingTerms = r.clarifications.map((c) => c.term);
    applyResult(r);
    renderTelemetry(r.telemetry, r.request_id);
    renderAssumptions(r.assumptions);
    $("approve-msg").textContent = "";
  } catch (e) {
    const detail = typeof e.data?.detail === "string" ? e.data.detail : e.message;
    $("interpret-error").textContent = `Request failed: ${detail}${e.data?.request_id ? ` (request ${e.data.request_id})` : ""}`;
    $("interpret-error").hidden = false;
  } finally {
    btn.disabled = false; btn.textContent = "Interpret";
  }
}

function applyResult(r) {
  state.status = r.status;
  state.definition = r.definition;
  state.pendingTerms = r.clarifications.map((c) => c.term);
  $("result").classList.remove("hidden");
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
      b.className = "option"; b.textContent = o.label;
      b.onclick = () => resolve(c.term, o.id);
      div.appendChild(b);
    });
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
  $("ack").onchange = (e) => { state.acknowledged = e.target.checked; renderBanner(); updateApprove(); if (state.acknowledged) size(); };
}

function sourceBadge(source) {
  if (source === "model") return '<span class="badge model">AI</span>';
  if (source === "user") return '<span class="badge user">edited</span>';
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
  $("result").classList.remove("hidden");
  recheck();
}

async function resolve(term, optionId) {
  await recheck({ [term]: optionId });
}

async function recheck(resolutions = {}) {
  const r = await api("/api/check", { definition: state.definition, pending_terms: state.pendingTerms, resolutions });
  applyResult({ ...r, unmapped_phrases: state.unmapped });
}

async function size() {
  try {
    const s = await api("/api/size", state.definition);
    state.sized = s;
    $("size-card").classList.remove("hidden");
    $("approve-card").classList.remove("hidden");
    $("reachable").textContent = fmt(s.reachable);
    $("matched").textContent = fmt(s.matched);
    $("excluded").textContent = fmt(s.excluded_no_consent);
    $("share").textContent = (s.share_of_base * 100).toFixed(1) + "%";
    renderBars($("by-region"), s.by_region, s.reachable);
    renderBars($("by-line-type"), s.by_line_type, s.reachable);
    renderIssues($("size-warnings"), s.warnings);
    $("size-meta").textContent = `Counted over ${fmt(s.base)} lines in ${s.query_ms} ms · one row per line, consent applied for ${state.definition.channel.toUpperCase()}.`;
  } catch (e) {
    hideSize();
  }
  updateApprove();
}

function hideSize() {
  state.sized = null;
  $("size-card").classList.add("hidden");
  $("approve-card").classList.add("hidden");
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
  const parts = [`request ${requestId}`, `model ${t.model || "n/a"}`, `${(t.total_ms / 1000).toFixed(2)} s`];
  if (t.llm_calls) parts.push(`${t.llm_calls} AI call${t.llm_calls > 1 ? "s" : ""}`, `${fmt(t.input_tokens + t.cache_read_tokens + t.cache_write_tokens)} in / ${fmt(t.output_tokens)} out tokens`, `$${t.cost_usd.toFixed(4)}`);
  if (t.cache_hit) parts.push("cached answer");
  if (t.repaired) parts.push("self-corrected once");
  if (t.pii_masked) parts.push(`${t.pii_masked} personal detail(s) masked`);
  $("telemetry").textContent = parts.join(" · ");
}

function updateApprove() {
  const ok = state.sized && displayStatus() === "ready" && !state.pendingTerms.length
    && $("aud-name").value.trim().length >= 3 && $("approver").value.trim().length >= 2;
  $("approve-btn").disabled = !ok;
}

async function approve() {
  try {
    const r = await api("/api/approve", {
      request_id: state.requestId, name: $("aud-name").value, approver: $("approver").value,
      definition: state.definition, acknowledged_unmapped: state.acknowledged,
    });
    $("approve-msg").textContent = `Approved as audience #${r.id} · ${fmt(r.reachable)} reachable lines · definition ${r.definition_hash}`;
    loadAudit();
    loadInsights();
  } catch (e) {
    $("approve-msg").textContent = "Not approved: " + JSON.stringify(e.data?.detail || e.message);
  }
}

async function loadAudit() {
  const rows = await api("/api/audiences");
  $("audit").innerHTML = rows.map((r) => `
    <tr><td>${r.id}</td><td>${esc(r.name)}</td><td>${fmt(r.reachable)}</td><td>${esc(r.approver)}</td>
    <td>${r.edited_by_user ? "yes" : "no"}</td><td class="def">${esc(r.explanation)}<div class="mono">${r.definition_hash} · ${esc(r.model || "manual")} · ${esc(r.created_at.replace("T", " ").replace("+00:00", ""))}</div></td>
    <td>${governanceCell(r)}</td></tr>`).join("")
    || '<tr><td colspan="7" class="hint">Nothing approved yet.</td></tr>';
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
    return `<li><strong>${esc(t.term)}</strong><span class="n">asked ${t.asked}×</span><div class="hint">${chosen || "no approvals yet"} · decides: ${esc(t.owner_pending)}</div></li>`;
  }).join("");
  $("ins-blocked").innerHTML = ins.blocked_by_policy.map((b) =>
    `<li>${esc(b.label)} <span class="hint">(${esc(b.policy)})</span><span class="n">${b.count}×</span></li>`).join("")
    || '<li class="hint">Nothing yet.</li>';
  const e = ins.edits;
  $("ins-edits").innerHTML = `<li>Approved audiences edited before approval<span class="n">${e.edited}/${e.approved}</span></li>`
    + e.by_field.map((f) => `<li>${esc(f.label)}<span class="n">${f.count}×</span></li>`).join("");
}

init();

// ASAS console: a thin client over /api. All rendering escapes text (alert explanations are
// untrusted input); all authorisation is enforced server-side. No inline styles (strict CSP):
// bar widths are set through the CSSOM after rendering.

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const chip = (v, cls = "") => `<span class="chip ${esc(cls || v)}">${esc(v)}</span>`;
const ANOMALY = new Set(["TYPED_ANOMALY", "UNEXPLAINED_DEVIATION"]);

let ME = null;       // /api/whoami
let CONTEXT = null;  // /api/context (business meaning of every code)

function identity() {
  const [user, role] = ($("#identity").value || "viewer.ann|viewer").split("|");
  return { user, role };
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (!ME || ME.auth_mode === "dev") {  // real deployments authenticate at the proxy / IdP
    const { user, role } = identity();
    headers["X-User"] = user;
    headers["X-Roles"] = role;
  }
  const res = await fetch(path, { ...options, headers });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || body.error || res.statusText);
  return body;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 3500);
}

async function act(label, fn) {
  try {
    const out = await fn();
    toast(`${label}: done`);
    return out;
  } catch (err) {
    toast(`${label}: ${err.message}`);
    return null;
  }
}

function sizeBars(root) {
  root.querySelectorAll(".bar > span[data-w]").forEach((s) => {
    s.style.width = `${Math.max(0, Math.min(100, Number(s.dataset.w) || 0))}%`;
  });
}

const meaning = (kind, code) => {
  if (!CONTEXT || !CONTEXT[kind]) return "";
  const entry = CONTEXT[kind][code] || CONTEXT[kind][String(code).split(":")[0]];
  return entry ? entry.meaning || entry.why_it_matters || "" : "";
};
const title = (kind, code) => (CONTEXT && CONTEXT[kind] && CONTEXT[kind][code] && CONTEXT[kind][code].title) || code;
const table = (head, rows) => `<div class="table-wrap"><table><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>${rows}</table></div>`;

function show(view) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $(`#view-${view}`).classList.add("active");
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  const loaders = { overview, cases, links, challenge, evolution, policy, data, audit };
  if (loaders[view]) loaders[view]().catch((err) => { $(`#view-${view}`).innerHTML = `<div class="banner">${esc(err.message)}</div>`; });
}

// ------------------------------------------------------------------ overview

function statusBanners(o, r) {
  const out = [];
  if (o.ops && o.ops.bulk_suspended) out.push(`<div class="banner">Safe mode: bulk proposals suspended by ${esc(o.ops.actor)} (${esc(o.ops.reason)})</div>`);
  if (o.ops && o.ops.llm_suspended) out.push(`<div class="banner">Safe mode: LLM suspended; agents run on the deterministic playbook (${esc(o.ops.reason)})</div>`);
  if (r && r.data_gates && r.data_gates.length) out.push(`<div class="banner">Data gate failed, bulk blocked for this run: ${r.data_gates.map(esc).join("; ")}</div>`);
  if (o.drift && o.drift.status === "DRIFT") out.push(`<div class="banner">Drift versus recent runs: ${o.drift.flags.map((f) => esc(`${f.metric} ${f.baseline} → ${f.current}`)).join("; ")}</div>`);
  if (!o.audit.valid) out.push(`<div class="banner">AUDIT CHAIN BROKEN</div>`);
  if (r && r.unavailable_fields && r.unavailable_fields.length) out.push(`<div class="banner quiet">Fields unavailable in the data (dependent checks abstain): ${r.unavailable_fields.map(esc).join(", ")}</div>`);
  return out.join("");
}

function opsPanel(o) {
  if (!ME || !ME.roles.includes("admin")) return "";
  const s = o.ops || {};
  return `<div class="card mt"><h3>Operations: safe mode (audited)</h3>
    <div class="row">
      <label class="check"><input type="checkbox" id="ops-bulk" ${s.bulk_suspended ? "checked" : ""}> suspend bulk proposals</label>
      <label class="check"><input type="checkbox" id="ops-llm" ${s.llm_suspended ? "checked" : ""}> suspend LLM (playbook only)</label>
      <input type="text" id="ops-reason" placeholder="reason (required)" size="36">
      <button class="btn danger" id="ops-apply">Apply</button>
    </div></div>`;
}

async function overview() {
  const el = $("#view-overview");
  el.innerHTML = "<p class='muted'>Loading...</p>";
  const [o, evaluation] = await Promise.all([api("/api/overview"), api("/api/evaluation").catch(() => ({}))]);
  const r = o.latest_run;
  const kpi = (label, value, note = "", red = false) => `<div class="card${red ? " alert" : ""}"><h3>${esc(label)}</h3>
      <div class="kpi${red ? " red" : ""}">${esc(value)}</div><div class="muted">${esc(note)}</div></div>`;
  let evalHtml = "";
  if (evaluation && evaluation.treatment) {
    const rows = (obj) => Object.entries(obj || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${esc(v)}</td></tr>`).join("");
    evalHtml = `<h2 class="mt">Evaluation against ground truth (synthetic)</h2><div class="two">
      <table><tr><th colspan=2>Detection</th></tr>${rows(evaluation.detection)}</table>
      <table><tr><th colspan=2>Treatment</th></tr>${rows(evaluation.treatment)}</table>
      <table><tr><th colspan=2>Anomaly ranking: raw vs explained-away outlyingness</th></tr>${rows(evaluation.anomaly_ranking)}</table>
      <table><tr><th colspan=2>Linking</th></tr>${rows(evaluation.linking)}</table></div>`;
  }
  el.innerHTML = `
    ${statusBanners(o, r)}
    <div class="toolbar">
      <button class="btn primary" id="run-pipeline">Run pipeline</button>
      <button class="btn" id="resolve-links">Investigate unresolved relationships</button>
      <span class="muted">Policy ${esc(o.active_bundle)} &middot; ${esc(o.rules)} rules &middot;
      LLM ${o.agents.llm_enabled ? "on (" + esc(o.agents.model) + ")" : "off (deterministic playbook)"}</span>
    </div>
    <div class="grid">
      ${kpi("Cases in review window", r ? r.cases : "-", r ? "as of " + r.as_of : "no run yet")}
      ${kpi("Escalation recommended", r ? r.escalation : "-", "verified anomaly or high unexplained deviation", r && r.escalation > 0)}
      ${kpi("Proposed for bulk attestation", r ? r.proposed_bulk : "-", r ? r.cohorts + " cohorts; never closed automatically" : "")}
      ${kpi("Abstained", r ? r.abstained : "-", "missing evidence: individual review")}
      ${kpi("Agent failures", r ? r.agent_failures : "-", "each fell back to individual review", r && r.agent_failures > 0)}
      ${kpi("Audit chain", o.audit.valid ? "valid" : "BROKEN", o.audit.entries + " entries", !o.audit.valid)}
    </div>
    ${opsPanel(o)}
    <h2 class="mt">Candidates</h2>
    <div>${Object.entries(o.candidates).map(([id, s]) => chip(id + " " + s, s)).join(" ") || "<span class='muted'>none yet</span>"}</div>
    ${evalHtml}`;
  $("#run-pipeline").onclick = async () => { await act("Pipeline", () => api("/api/pipeline/run", { method: "POST", body: "{}" })); overview(); };
  $("#resolve-links").onclick = async () => { await act("Relationship investigation", () => api("/api/links/resolve", { method: "POST", body: "{}" })); };
  const apply = $("#ops-apply");
  if (apply) apply.onclick = async () => {
    const body = { bulk_suspended: $("#ops-bulk").checked, llm_suspended: $("#ops-llm").checked, reason: $("#ops-reason").value };
    await act("Safe mode", () => api("/api/ops", { method: "POST", body: JSON.stringify(body) }));
    overview();
  };
}

// ------------------------------------------------------------------ cases

async function cases() {
  const el = $("#view-cases");
  const filter = el.dataset.filter || "";
  const rows = await api("/api/cases" + (filter ? `?recommendation=${filter}` : ""));
  el.innerHTML = `
    <div class="toolbar">
      <label>Recommendation <select id="case-filter">
        ${["", "PROPOSED_BULK", "INDIVIDUAL_REVIEW", "ESCALATION_RECOMMENDED"].map((v) =>
          `<option value="${v}" ${v === filter ? "selected" : ""}>${v || "all"}</option>`).join("")}
      </select></label>
      <span class="muted">${rows.length} cases in queue order (bucket, score, age)</span>
    </div>
    ${table(["Case", "Desk", "Recommendation", "Classification", "Severity", "Confidence", "Outlyingness", "Conclusion", "Reasons"],
      rows.map((c) => `<tr class="clickable${ANOMALY.has(c.category) ? " risk" : ""}" data-id="${esc(c.case_id)}">
        <td>${esc(c.case_id)}</td><td>${esc(c.desk)}</td><td>${chip(c.recommendation)}</td>
        <td>${c.category ? chip(c.category) : ""}</td><td>${c.severity ? chip(c.severity) : ""}</td>
        <td>${c.confidence ? chip(c.confidence, "quiet") : ""}</td>
        <td class="num">${esc(c.outlyingness ?? "")} ${c.residual_band && c.residual_band !== "NONE" ? chip("unexplained " + c.residual_band, "risk") : ""}</td>
        <td>${esc(c.conclusion || "-")}</td>
        <td>${c.reasons.map((x) => chip(x, "reason")).join("")}</td></tr>`).join(""))}`;
  $("#case-filter").onchange = (e) => { el.dataset.filter = e.target.value; cases(); };
  el.querySelectorAll("tr.clickable").forEach((tr) => { tr.onclick = () => caseDetail(tr.dataset.id); });
}

function classificationCard(cls, ctx) {
  if (!cls) return `<div class="card"><h3>Classification</h3><span class="muted">not classified</span></div>`;
  const lines = cls.corroboration.map((l) => `<li><span class="strong">${esc(l)}</span> <span class="muted">${esc(meaning("corroboration", l))}</span></li>`).join("");
  return `<div class="card${ANOMALY.has(cls.category) ? " alert" : ""}"><h3>Classification</h3>
    <div class="row">${chip(cls.category)} ${chip("severity " + cls.severity, cls.severity)} ${chip("confidence " + cls.confidence, "quiet")}</div>
    <h4>${esc(title("hypotheses", cls.typology))}</h4>
    <div class="muted">${esc(ctx.category || "")}</div>
    <div class="label mt">Lines of evidence (${esc(cls.corroboration.length)})</div><ul class="plain">${lines || "<li class='muted'>none</li>"}</ul></div>`;
}

function contextCard(ctx) {
  const c = ctx.conclusion;
  if (!c) return `<div class="card"><h3>Business context</h3><span class="muted">No conclusion: see the missing evidence.</span></div>`;
  return `<div class="card"><h3>Business context</h3>
    <h4 class="m0">${esc(c.title)}</h4><div class="muted">${esc(c.risk_theme)}</div>
    <p>${esc(c.meaning)}</p><p class="muted">${esc(c.why_it_matters)}</p>
    <div class="label">Reviewer checks</div><ul class="plain">${(c.reviewer_checks || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>`;
}

function anomalyCard(dev, cls, inv) {
  if (!dev) return `<div class="card"><h3>Anomaly analysis</h3><span class="muted">not profiled</span></div>`;
  const unexplained = new Set(cls ? cls.unexplained : []);
  const explained = new Set(cls ? cls.explained : []);
  const pending = new Set(inv ? inv.pending_deviations : []);
  const maxTotal = Math.max(Number(dev.outlyingness) || 0, 2.5);
  const bars = dev.components.map((c) => `<div>${esc(c.name)}</div>
      <div class="bar"><span data-w="${esc((Number(c.contribution) / maxTotal) * 100)}"></span></div>
      <div class="num">${esc(c.contribution)}</div>`).join("");
  const residual = cls ? `<div class="strong">unexplained</div>
      <div class="bar"><span class="risk" data-w="${esc((Number(cls.residual_outlyingness) / maxTotal) * 100)}"></span></div>
      <div class="num red">${esc(cls.residual_outlyingness)}</div>` : "";
  const status = (s) => unexplained.has(s) ? chip("UNEXPLAINED", "risk") : explained.has(s) ? chip("explained", "strong") : pending.has(s) ? chip("awaiting evidence", "quiet") : "";
  const rows = dev.deviations.map((d) => `<tr class="${d.verified && unexplained.has(d.signal) ? "risk" : ""}">
      <td><span class="strong">${esc(title("signals", d.signal))}</span><div class="faint">${esc(d.signal)}</div></td>
      <td class="num">${esc(d.value)}</td>
      <td>${d.verified ? chip("VERIFIED") : chip("not verified", "quiet")} ${status(d.signal)}</td>
      <td>${d.peers.map((p) => `<div>${p.deviant ? "<span class='red'>&#9679;</span>" : "&#9675;"} ${esc(p.level)} n=${esc(p.n)} p${esc(p.percentile)} z${esc(p.robust_z)}</div>`).join("")}</td>
      <td>${d.self_history ? `p${esc(d.self_history.percentile)} (n=${esc(d.self_history.n)})` : "<span class='faint'>n/a</span>"}</td>
      <td>${d.stable === null ? "<span class='faint'>untestable</span>" : d.stable ? "stable" : "<span class='red'>unstable</span>"}</td>
      <td class="faint">${d.reasons.map(esc).join(", ")}</td></tr>`).join("");
  const joint = dev.joint ? `<div class="mt-s">${dev.joint.verified ? chip("RARE COMBINATION", "risk") : chip("combination", "quiet")}
      ${dev.joint.items.map((i) => chip(i, "reason")).join("")} <span class="muted">seen ${esc(dev.joint.support)} times in ${esc(dev.joint.reference_n)} other episodes</span>
      ${status("joint")}</div>` : "";
  return `<div class="card${unexplained.size ? " alert" : ""}"><h3>Anomaly analysis: verified deviation vs peers</h3>
    <div class="row">${chip("outlyingness " + dev.outlyingness + " " + dev.band, dev.band)} ${cls ? chip("unexplained " + cls.residual_outlyingness + " " + cls.residual_band, cls.residual_band) : ""}
      <span class="muted">screened ${esc(dev.screened.length)} signals${dev.notes.length ? " · " + dev.notes.map(esc).join(", ") : ""}</span></div>
    <div class="bars mt">${bars}${residual}</div>
    ${rows ? table(["Signal", "Value", "Status", "Peers (level, size, percentile, robust z)", "Own book", "Stability", "Verification"], rows) : "<p class='muted'>No signal deviates from its peers.</p>"}
    ${joint}</div>`;
}

async function caseDetail(caseId) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  const el = $("#view-case");
  el.classList.add("active");
  el.innerHTML = "<p class='muted'>Loading...</p>";
  const d = await api(`/api/cases/${encodeURIComponent(caseId)}`);
  const inv = d.investigation;
  const c = d.case;
  const ctx = d.context || {};
  const hyps = inv ? inv.hypotheses.map((h) => `
      <details ${h.status === "SUPPORTED" ? "open" : ""}><summary>${chip(h.status)} ${esc(title("hypotheses", h.type))} <span class="faint">${esc(h.klass)}</span></summary>
        <div class="muted">${esc(h.rationale)}</div>
        ${h.supporting.length ? "<div><span class='strong'>Supporting:</span> " + h.supporting.map(esc).join("; ") + "</div>" : ""}
        ${h.contradicting.length ? "<div><span class='strong'>Contradicting:</span> " + h.contradicting.map(esc).join("; ") + "</div>" : ""}
        ${h.missing.length ? "<div><span class='strong red'>Missing:</span> " + h.missing.map(esc).join("; ") + "</div>" : ""}
        <div class="faint">evidence ${h.evidence_ids.map(esc).join(", ")}</div></details>`).join("") : "<p class='muted'>Not investigated.</p>";
  const evidence = inv ? inv.evidence.map((e) => `<tr><td>${esc(e.evidence_id)}</td><td>${esc(e.tool)}</td>
      <td>${esc(Object.entries(e.args).map(([k, v]) => k + "=" + v).join(", "))}</td>
      <td>${esc(Object.entries(e.facts).slice(0, 8).map(([k, v]) => k + ": " + v).join(" | "))}${e.lead_only ? " " + chip("lead only", "quiet") : ""}</td></tr>`).join("") : "";
  const components = c.score.components.map((x) => `<tr><td>${esc(x.name)}</td><td class="num">${esc(x.contribution)}</td><td>${esc(x.detail)}</td></tr>`).join("");
  const seq = d.sequence.events.map((e) => `<tr><td class="num">${esc(e.hours_from_start)}h</td><td>${esc(e.trade_id)} v${esc(e.version)}</td><td>${esc(e.event_type)}${e.is_rebook ? " (rebook)" : ""}</td><td class="num">${esc(e.price ?? "")}</td><td class="num">${esc(e.quantity ?? "")}</td><td>${esc(e.side ?? "")}</td></tr>`).join("");
  const links = d.episode.links.map((l) => `<div>${chip(l.status)} ${esc(l.src)} &rarr; ${esc(l.dst)} ${esc(l.kind)} <span class="faint">tier ${esc(l.tier)}, ${esc(l.reason)}</span></div>`).join("") || "<span class='muted'>single-trade episode</span>";
  const proposals = inv ? inv.link_proposals.map((p) => `<div>${chip(p.status)} ${esc(p.trade_b)} rebooks ${esc(p.trade_a)}
      ${p.status === "VERIFIED" ? `<button class="btn" data-confirm="${esc(p.proposal_id)}">Confirm link (human)</button>` : ""}</div>`).join("") : "";
  const reasons = c.reasons.length
    ? `<ul class="plain">${c.reasons.map((x) => `<li>${chip(x, "reason")} <span class="muted">${esc((ctx.reasons || {})[x] || meaning("reasons", x))}</span></li>`).join("")}</ul>`
    : "<p>All gates passed: a verified benign explanation, nothing unexplained, inside the approved bulk policy.</p>";
  el.innerHTML = `
    <div class="toolbar"><button class="btn" id="back">&larr; Cases</button>
      <h2 class="m0">${esc(c.case_id)}</h2>${chip(c.recommendation)} ${c.control_sample ? chip("control sample", "quiet") : ""}
      <span class="spacer"></span>
      <button class="btn" id="reinvestigate">Re-run investigation</button>
      <button class="btn" data-decision="CLEARED">Record decision: cleared</button>
      <button class="btn danger" data-decision="ESCALATED">Record decision: escalated</button>
    </div>
    <div class="three">
      ${classificationCard(c.classification, ctx)}
      <div class="card"><h3>Why this recommendation</h3><div class="muted">${esc(ctx.recommendation || "")}</div>${reasons}</div>
      ${contextCard(ctx)}
    </div>
    <div class="mt">${anomalyCard(c.deviation, c.classification, inv)}</div>
    <div class="two mt">
      <div class="card"><h3>Hypotheses considered (verified deterministically)</h3>${hyps}</div>
      <div class="card"><h3>Investigation narrative (evidence-cited)</h3>
        <pre>${esc(inv ? inv.explanation : "")}</pre>
        <div class="faint">${inv ? `policy ${esc(inv.policy)}${inv.fallback_used ? " (fell back to playbook)" : ""} &middot; ${esc(inv.steps)} steps &middot; ${esc(inv.tool_calls)} tool calls &middot; run ${esc(inv.run_id)}` : ""}</div>
        <h3 class="mt">Why these events were linked</h3>${links}
        ${proposals ? "<h3 class='mt'>Agent-proposed relationships (quarantined until a human confirms)</h3>" + proposals : ""}
        <h3 class="mt">Event sequence</h3>${table(["t", "trade", "event", "price", "qty", "side"], seq)}</div>
    </div>
    <div class="two mt">
      <div class="card"><h3>Attention score (named components; orders work, never decides)</h3>
        ${table(["component", "contribution", "detail"], components)}
        <h3 class="mt">Alerts (existing rule behaviour)</h3>
        ${d.alerts.map((a) => `<div>${chip(a.rule_id, "reason")} ${esc(a.alert_id)} on ${esc(a.trade_id)} v${esc(a.trade_version)}<pre>${esc(a.explanation_untrusted || "(no explanation)")}</pre></div>`).join("")}</div>
      <div class="card"><h3>Evidence retrieved by the agent</h3>${table(["id", "tool", "args", "facts"], evidence)}</div>
    </div>
    <div class="card mt"><h3>Evidence graph neighbourhood</h3>
      <div class="muted">${esc(d.graph.nodes.length)} nodes, ${esc(d.graph.edges.length)} edges${d.graph.truncated ? " (truncated)" : ""}</div>
      <div>${d.graph.edges.slice(0, 40).map((e) => `<div>${chip(e.status)} ${esc(e.src)} &mdash;${esc(e.kind)}&rarr; ${esc(e.dst)} <span class="faint">${esc(e.provenance)}</span></div>`).join("")}</div></div>`;
  sizeBars(el);
  $("#back").onclick = () => show("cases");
  $("#reinvestigate").onclick = async () => { await act("Investigation", () => api(`/api/cases/${encodeURIComponent(caseId)}/investigate`, { method: "POST" })); caseDetail(caseId); };
  el.querySelectorAll("[data-decision]").forEach((b) => {
    b.onclick = () => act("Decision recorded", () => api(`/api/cases/${encodeURIComponent(caseId)}/decision`, { method: "POST", body: JSON.stringify({ label: b.dataset.decision }) }));
  });
  el.querySelectorAll("[data-confirm]").forEach((b) => {
    b.onclick = async () => { await act("Link confirmed", () => api(`/api/links/${encodeURIComponent(b.dataset.confirm)}/confirm`, { method: "POST" })); caseDetail(caseId); };
  });
}

// ------------------------------------------------------------------ relationships

async function links() {
  const el = $("#view-links");
  const rows = await api("/api/links");
  el.innerHTML = `<h2>Agent-proposed relationships</h2>
    <p class="muted">Deterministic linking is canonical. Agent proposals are verified deterministically, stay quarantined, and become canonical only when a human confirms them.</p>
    ${table(["Proposal", "Cancelled", "Rebook", "Status", "Verification", ""],
      rows.map((p) => `<tr><td>${esc(p.proposal_id)}</td><td>${esc(p.trade_a)}</td><td>${esc(p.trade_b)}</td><td>${chip(p.status)}</td>
      <td>${Object.entries(p.verification).map(([k, v]) => chip(k + "=" + v, v === "false" ? "risk" : "quiet")).join("")}</td>
      <td>${p.status === "VERIFIED" ? `<button class="btn" data-confirm="${esc(p.proposal_id)}">Confirm</button>` : ""}</td></tr>`).join(""))}`;
  el.querySelectorAll("[data-confirm]").forEach((b) => {
    b.onclick = async () => { await act("Link confirmed", () => api(`/api/links/${encodeURIComponent(b.dataset.confirm)}/confirm`, { method: "POST" })); links(); };
  });
}

// ------------------------------------------------------------------ challenger

async function challenge() {
  const el = $("#view-challenge");
  const report = await api("/api/challenge");
  const findings = report.findings || [];
  el.innerHTML = `<div class="toolbar"><button class="btn primary" id="run-challenge">Challenge existing detections</button>
      <span class="muted">${findings.length} findings; blind spots are examined first, most outlying first</span></div>
    ${table(["Kind", "Status", "Episode", "Rules", "Facts", "Summary"],
      findings.map((f) => `<tr class="${f.kind === "BLIND_SPOT" && f.status === "CONFIRMED" ? "risk" : ""}"><td>${chip(f.kind, f.kind === "BLIND_SPOT" ? "risk" : "quiet")}<div class="faint">${esc(meaning("findings", f.kind))}</div></td><td>${chip(f.status)}</td><td>${esc(f.episode_id)}</td>
      <td>${f.rule_ids.map((r) => chip(r, "reason")).join("")}</td>
      <td>${esc(Object.entries(f.facts).map(([k, v]) => k + ": " + v).join(" | "))}</td><td>${esc(f.summary)}</td></tr>`).join(""))}`;
  $("#run-challenge").onclick = async () => { await act("Challenger", () => api("/api/challenge", { method: "POST", body: "{}" })); challenge(); };
}

// ------------------------------------------------------------------ discovery + governance

async function evolution() {
  const el = $("#view-evolution");
  const [patterns, candidates] = await Promise.all([api("/api/patterns"), api("/api/candidates")]);
  el.innerHTML = `<div class="toolbar"><button class="btn primary" id="run-discover">Discover patterns</button>
      <span class="muted">candidate &rarr; replay &rarr; counterexamples &rarr; shadow &rarr; submit &rarr; four-eyes approval &rarr; versioned release</span></div>
    <h2>Patterns</h2>${table(["Kind", "Items", "Support", "Escalated", "Cleared", "Rule coverage"],
      patterns.map((p) => `<tr><td>${chip(p.kind, p.kind === "UNCAPTURED_RISK" ? "risk" : "quiet")}</td><td>${p.items.map((i) => chip(i, "reason")).join("")}</td><td class="num">${esc(p.support)}</td>
        <td class="num">${esc(p.escalated)}</td><td class="num">${esc(p.cleared)}</td><td class="num">${esc(p.rule_coverage)}</td></tr>`).join(""))}
    <h2 class="mt">Candidates</h2>
    ${candidates.map((c) => `<div class="card mt">
      <div class="toolbar"><span class="strong">${esc(c.candidate.candidate_id)}</span> ${chip(c.candidate.kind, "quiet")} ${chip(c.state)}
        ${["replay", "counterexamples", "shadow", "submit", "approve", "reject"].map((s) =>
          `<button class="btn ${s === "reject" ? "danger" : ""}" data-step="${s}" data-id="${esc(c.candidate.candidate_id)}">${s}</button>`).join("")}
        <button class="btn" data-view-cand="${esc(c.candidate.candidate_id)}">details</button></div>
      <div class="muted">${esc(c.candidate.rationale)}</div>
      <pre>${esc(c.candidate.rule ? JSON.stringify(c.candidate.rule.conditions) : JSON.stringify(c.candidate.bulk_scope))}</pre>
      <div id="cand-${esc(c.candidate.candidate_id)}"></div></div>`).join("") || "<p class='muted'>No candidates yet.</p>"}`;
  $("#run-discover").onclick = async () => { await act("Discovery", () => api("/api/discover", { method: "POST", body: "{}" })); evolution(); };
  el.querySelectorAll("[data-step]").forEach((b) => {
    b.onclick = async () => {
      const note = b.dataset.step === "reject" ? "rejected in console" : "via console";
      await act(b.dataset.step, () => api(`/api/candidates/${encodeURIComponent(b.dataset.id)}/${b.dataset.step}`, { method: "POST", body: JSON.stringify({ note }) }));
      evolution();
    };
  });
  el.querySelectorAll("[data-view-cand]").forEach((b) => {
    b.onclick = async () => {
      const d = await api(`/api/candidates/${encodeURIComponent(b.dataset.viewCand)}`);
      $(`#cand-${CSS.escape(b.dataset.viewCand)}`).innerHTML = `
        <h3>Events</h3>${d.events.map((e) => `<div>${chip(e.state)} ${esc(e.actor)} ${esc(e.at)} ${esc(e.note)}</div>`).join("")}
        <h3 class="mt">Artifacts</h3><pre>${esc(JSON.stringify(d.artifacts, null, 1))}</pre>`;
    };
  });
}

// ------------------------------------------------------------------ policy, data, audit

async function policy() {
  const el = $("#view-policy");
  const p = await api("/api/policy");
  el.innerHTML = `<h2>Active policy bundle: ${esc(p.active?.bundle_id)}</h2>
    <div class="two"><div class="card"><h3>Detection rules</h3>
      ${(p.active?.ruleset.rules || []).map((r) => `<div>${chip(r.subrule_id, "strong")} ${esc(r.description)}<pre>${esc(JSON.stringify(r.conditions))}</pre></div>`).join("")}</div>
      <div class="card"><h3>Bulk-review policy</h3>${(p.active?.bulk_policy.allowed || []).map((s) => chip(s.hypothesis_type + " @ " + s.desk, "reason")).join("")}</div></div>
    <h2 class="mt">Version history</h2>${table(["Bundle", "Parent", "Created by", "From candidate", "Rules", "Bulk scopes", ""],
      p.bundles.map((b) => `<tr><td>${esc(b.bundle_id)}</td><td>${esc(b.parent_id || "")}</td><td>${esc(b.created_by)}</td>
        <td>${esc(b.source_candidate || "")}</td><td class="num">${esc(b.rules)}</td><td class="num">${esc(b.bulk_scopes)}</td>
        <td><button class="btn" data-rollback="${esc(b.bundle_id)}">activate</button></td></tr>`).join(""))}`;
  el.querySelectorAll("[data-rollback]").forEach((b) => {
    b.onclick = async () => { await act("Activation", () => api("/api/policy/rollback", { method: "POST", body: JSON.stringify({ bundle_id: b.dataset.rollback, reason: "console activation" }) })); policy(); };
  });
}

async function data() {
  const el = $("#view-data");
  const d = await api("/api/data/capabilities");
  const m = d.matrix;
  const rows = (obj, kind) => Object.entries(obj).map(([k, v]) => `<tr><td>${esc(title(kind, k))}${title(kind, k) !== k ? `<div class="faint">${esc(k)}</div>` : ""}</td>
      <td>${chip(String(v).split(":")[0], String(v).startsWith("ENABLED") ? "strong" : "risk")} <span class="muted">${esc(String(v).split(":").slice(1).join(":"))}</span></td></tr>`).join("");
  const run = d.latest_run;
  el.innerHTML = `<h2>Data capabilities <span class="muted">as of ${esc(d.as_of)}</span></h2>
    <p class="muted">What ASAS can do with the fields actually present. Anything DEGRADED abstains to individual review instead of guessing. Generated from the same capability model the engine uses.</p>
    ${run && run.data_gates.length ? `<div class="banner">Latest run data gates failed: ${run.data_gates.map(esc).join("; ")}</div>` : ""}
    <div class="three">
      <div class="card"><h3>Measure-field coverage</h3>${table(["field", "share"], Object.entries(m.coverage).map(([k, v]) => `<tr class="${m.unavailable_fields.includes(k) ? "risk" : ""}"><td>${esc(k)}</td><td class="num">${esc(v)}</td></tr>`).join(""))}
        <div class="mt-s muted">unavailable: ${m.unavailable_fields.map(esc).join(", ") || "none"}</div></div>
      <div class="card"><h3>Rows</h3>${table(["entity", "rows"], Object.entries(m.entities).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${esc(v)}</td></tr>`).join(""))}</div>
      <div class="card"><h3>Production rules that cannot fire</h3>${m.rule_gaps.length ? m.rule_gaps.map((g) => chip(g, "risk")).join("") : "<span class='muted'>none</span>"}
        <h3 class="mt">Link keys present</h3>${table(["key", "rows"], Object.entries(m.link_key_rows).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${esc(v)}</td></tr>`).join(""))}</div>
    </div>
    <div class="three mt">
      <div class="card"><h3>Hypotheses</h3>${table(["hypothesis", "status"], rows(m.hypotheses, "hypotheses"))}</div>
      <div class="card"><h3>Deviation signals</h3>${table(["signal", "status"], rows(m.deviation_signals, "signals"))}</div>
      <div class="card"><h3>Platform features</h3>${table(["feature", "status"], rows(m.features, "features"))}</div>
    </div>`;
}

async function audit() {
  const el = $("#view-audit");
  const a = await api("/api/audit?limit=200");
  el.innerHTML = `<h2>Audit chain ${a.valid ? chip("VALID") : chip("BROKEN")} <span class="muted">${esc(a.entries)} entries, hash-linked, append-only</span></h2>
    ${table(["#", "At", "Actor", "Action", "Subject", "Detail", "Hash"],
      a.recent.map((r) => `<tr><td class="num">${esc(r.seq)}</td><td>${esc(r.at)}</td><td>${esc(r.actor)}</td><td>${esc(r.action)}</td>
      <td>${esc(r.subject)}</td><td>${esc(JSON.stringify(r.detail))}</td><td class="faint">${esc(r.hash.slice(0, 12))}</td></tr>`).join(""))}`;
}

// ------------------------------------------------------------------ bootstrap

async function whoami() {
  try {
    ME = await api("/api/whoami");
  } catch (err) {
    ME = null;
    $("#who").textContent = "not signed in";
    return;
  }
  $("#dev-switch").hidden = ME.auth_mode !== "dev";
  $("#who").textContent = `${ME.user} · ${ME.roles.join(", ")}${ME.auth_mode === "dev" ? "" : " · " + ME.auth_mode}`;
}

document.querySelectorAll("#tabs button").forEach((b) => { b.onclick = () => show(b.dataset.view); });
try { const saved = localStorage.getItem("asas.identity"); if (saved) $("#identity").value = saved; } catch { /* storage unavailable */ }
$("#identity").onchange = async (e) => {
  try { localStorage.setItem("asas.identity", e.target.value); } catch { /* ignore */ }
  await whoami();
  show(document.querySelector("#tabs button.active").dataset.view);
};
(async () => {
  await whoami();
  CONTEXT = await api("/api/context").catch(() => null);
  show("overview");
})();

// ASAS console: a thin client over /api. All rendering escapes text (alert explanations are
// untrusted input); all authorisation is enforced server-side.

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const chip = (v, cls = "") => `<span class="chip ${esc(cls || v)}">${esc(v)}</span>`;

function identity() {
  const [user, role] = ($("#identity").value || "viewer.ann|viewer").split("|");
  return { user, role };
}

async function api(path, options = {}) {
  const { user, role } = identity();
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-User": user, "X-Roles": role, ...(options.headers || {}) },
  });
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

function show(view) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $(`#view-${view}`).classList.add("active");
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  const loaders = { overview, cases, links, challenge, evolution, policy, audit };
  if (loaders[view]) loaders[view]();
}

// ------------------------------------------------------------------ overview

async function overview() {
  const el = $("#view-overview");
  el.innerHTML = "<p class='muted'>Loading...</p>";
  const [o, evaluation] = await Promise.all([api("/api/overview"), api("/api/evaluation").catch(() => ({}))]);
  const r = o.latest_run;
  const kpi = (title, value, note = "") => `<div class="card"><h3>${esc(title)}</h3><div class="kpi">${esc(value)}</div><div class="muted">${esc(note)}</div></div>`;
  let evalHtml = "";
  if (evaluation && evaluation.treatment) {
    const rows = (obj) => Object.entries(obj).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
    evalHtml = `<h2>Evaluation against ground truth</h2><div class="two">
      <table><tr><th colspan=2>Detection</th></tr>${rows(evaluation.detection)}</table>
      <table><tr><th colspan=2>Treatment</th></tr>${rows(evaluation.treatment)}</table>
      <table><tr><th colspan=2>Linking</th></tr>${rows(evaluation.linking)}</table>
      <table><tr><th colspan=2>Score-only baseline</th></tr>${rows(evaluation.score_only_baseline)}</table></div>`;
  }
  el.innerHTML = `
    <div class="toolbar">
      <button class="btn primary" id="run-pipeline">Run pipeline</button>
      <button class="btn" id="resolve-links">Investigate unresolved relationships</button>
      <span class="muted">Active policy ${esc(o.active_bundle)} &middot; ${esc(o.rules)} rules &middot;
      LLM ${o.agents.llm_enabled ? "on (" + esc(o.agents.model) + ")" : "off (deterministic playbook)"}</span>
    </div>
    <div class="grid">
      ${kpi("Cases in review window", r ? r.cases : "-", r ? "as of " + r.as_of : "no run yet")}
      ${kpi("Proposed for bulk attestation", r ? r.proposed_bulk : "-", r ? r.cohorts + " cohorts" : "")}
      ${kpi("Escalation recommended", r ? r.escalation : "-", "anomaly supported by verified evidence")}
      ${kpi("Abstained", r ? r.abstained : "-", "insufficient evidence -> individual review")}
      ${kpi("Agent failures (fell back safely)", r ? r.agent_failures : "-")}
      ${kpi("Audit chain", o.audit.valid ? "valid" : "BROKEN", o.audit.entries + " entries")}
    </div>
    <h2>Candidates</h2>
    <div>${Object.entries(o.candidates).map(([id, s]) => chip(id + " " + s, s)).join(" ") || "<span class='muted'>none yet</span>"}</div>
    ${evalHtml}`;
  $("#run-pipeline").onclick = async () => { await act("Pipeline", () => api("/api/pipeline/run", { method: "POST", body: "{}" })); overview(); };
  $("#resolve-links").onclick = async () => { await act("Relationship investigation", () => api("/api/links/resolve", { method: "POST", body: "{}" })); };
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
      <span class="muted">${rows.length} cases, queue order (bucket, score, age)</span>
    </div>
    <div class="table-wrap"><table>
      <tr><th>Case</th><th>Desk</th><th>Recommendation</th><th>Verified conclusion</th><th>Bucket</th><th>Score</th><th>Reasons</th><th>Cohort</th></tr>
      ${rows.map((c) => `<tr class="clickable" data-id="${esc(c.case_id)}">
        <td>${esc(c.case_id)}</td><td>${esc(c.desk)}</td><td>${chip(c.recommendation)}</td>
        <td>${esc(c.conclusion || "-")}</td><td>${esc(c.bucket)}</td><td>${esc(c.score)}</td>
        <td>${c.reasons.map((x) => chip(x, "reason")).join("")}</td><td>${esc(c.cohort_id || "")}</td></tr>`).join("")}
    </table></div>`;
  $("#case-filter").onchange = (e) => { el.dataset.filter = e.target.value; cases(); };
  el.querySelectorAll("tr.clickable").forEach((tr) => { tr.onclick = () => caseDetail(tr.dataset.id); });
}

async function caseDetail(caseId) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  const el = $("#view-case");
  el.classList.add("active");
  el.innerHTML = "<p class='muted'>Loading...</p>";
  const d = await api(`/api/cases/${encodeURIComponent(caseId)}`);
  const inv = d.investigation;
  const c = d.case;
  const hyps = inv ? inv.hypotheses.map((h) => `
      <details ${h.status === "SUPPORTED" ? "open" : ""}><summary>${chip(h.status)} ${esc(h.type)} <span class="muted">(${esc(h.klass)})</span></summary>
        <div>${esc(h.rationale)}</div>
        ${h.supporting.length ? "<div><b>Supporting:</b> " + h.supporting.map(esc).join("; ") + "</div>" : ""}
        ${h.contradicting.length ? "<div><b>Contradicting:</b> " + h.contradicting.map(esc).join("; ") + "</div>" : ""}
        ${h.missing.length ? "<div><b>Missing:</b> " + h.missing.map(esc).join("; ") + "</div>" : ""}
        <div class="muted">evidence ${h.evidence_ids.map(esc).join(", ")}</div></details>`).join("") : "<p class='muted'>Not investigated.</p>";
  const evidence = inv ? inv.evidence.map((e) => `<tr><td>${esc(e.evidence_id)}</td><td>${esc(e.tool)}</td>
      <td>${esc(Object.entries(e.args).map(([k, v]) => k + "=" + v).join(", "))}</td>
      <td>${esc(Object.entries(e.facts).slice(0, 8).map(([k, v]) => k + ": " + v).join(" | "))}${e.lead_only ? " " + chip("lead only") : ""}</td></tr>`).join("") : "";
  const components = c.score.components.map((x) => `<tr><td>${esc(x.name)}</td><td>${esc(x.contribution)}</td><td>${esc(x.detail)}</td></tr>`).join("");
  const seq = d.sequence.events.map((e) => `<tr><td>${esc(e.hours_from_start)}h</td><td>${esc(e.trade_id)} v${esc(e.version)}</td><td>${esc(e.event_type)}${e.is_rebook ? " (rebook)" : ""}</td><td>${esc(e.price ?? "")}</td><td>${esc(e.quantity ?? "")}</td><td>${esc(e.side ?? "")}</td></tr>`).join("");
  const links = d.episode.links.map((l) => `<div>${chip(l.status)} ${esc(l.src)} &rarr; ${esc(l.dst)} ${esc(l.kind)} (tier ${esc(l.tier)}, ${esc(l.reason)})</div>`).join("") || "<span class='muted'>single-trade episode</span>";
  const proposals = inv ? inv.link_proposals.map((p) => `<div>${chip(p.status)} ${esc(p.trade_b)} rebooks ${esc(p.trade_a)}
      ${p.status === "VERIFIED" ? `<button class="btn" data-confirm="${esc(p.proposal_id)}">Confirm link (human)</button>` : ""}</div>`).join("") : "";
  el.innerHTML = `
    <div class="toolbar"><button class="btn" id="back">&larr; Cases</button>
      <h2 style="margin:0">${esc(c.case_id)}</h2>${chip(c.recommendation)} ${c.control_sample ? chip("control sample") : ""}
      <button class="btn" id="reinvestigate">Re-run investigation</button>
      <button class="btn" data-decision="CLEARED">Record decision: cleared</button>
      <button class="btn danger" data-decision="ESCALATED">Record decision: escalated</button>
    </div>
    <div class="two">
      <div class="card"><h3>Why this recommendation</h3>
        <div>${c.reasons.length ? c.reasons.map((x) => chip(x, "reason")).join("") : "<span class='PROPOSED_BULK'>All gates passed: verified benign explanation within bulk policy.</span>"}</div>
        <h3 style="margin-top:12px">Investigation narrative (deterministic, evidence-cited)</h3>
        <pre>${esc(inv ? inv.explanation : "")}</pre>
        <div class="muted">${inv ? `policy ${esc(inv.policy)}${inv.fallback_used ? " (fell back to playbook)" : ""} &middot; ${esc(inv.steps)} steps &middot; ${esc(inv.tool_calls)} tool calls &middot; run ${esc(inv.run_id)}` : ""}</div>
      </div>
      <div class="card"><h3>Hypotheses considered</h3>${hyps}</div>
    </div>
    <div class="two" style="margin-top:12px">
      <div class="card"><h3>Why these events were linked</h3>${links}${proposals ? "<h3 style='margin-top:10px'>Agent-proposed relationships (quarantined)</h3>" + proposals : ""}
        <h3 style="margin-top:10px">Event sequence</h3><div class="table-wrap"><table><tr><th>t</th><th>trade</th><th>event</th><th>price</th><th>qty</th><th>side</th></tr>${seq}</table></div></div>
      <div class="card"><h3>Attention score (named components, replaces model + SHAP)</h3>
        <table><tr><th>component</th><th>contribution</th><th>detail</th></tr>${components}</table>
        <h3 style="margin-top:10px">Alerts (existing rule behaviour)</h3>
        ${d.alerts.map((a) => `<div>${chip(a.rule_id)} ${esc(a.alert_id)} on ${esc(a.trade_id)} v${esc(a.trade_version)}<pre>${esc(a.explanation_untrusted || "(no explanation)")}</pre></div>`).join("")}</div>
    </div>
    <div class="card" style="margin-top:12px"><h3>Evidence retrieved by the agent</h3>
      <div class="table-wrap"><table><tr><th>id</th><th>tool</th><th>args</th><th>facts</th></tr>${evidence}</table></div></div>
    <div class="card" style="margin-top:12px"><h3>Evidence graph neighbourhood</h3>
      <div class="muted">${esc(d.graph.nodes.length)} nodes, ${esc(d.graph.edges.length)} edges${d.graph.truncated ? " (truncated)" : ""}</div>
      <div>${d.graph.edges.slice(0, 40).map((e) => `<div>${chip(e.status)} ${esc(e.src)} &mdash;${esc(e.kind)}&rarr; ${esc(e.dst)} <span class="muted">${esc(e.provenance)}</span></div>`).join("")}</div></div>`;
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
    <div class="table-wrap"><table><tr><th>Proposal</th><th>Cancelled</th><th>Rebook</th><th>Status</th><th>Verification</th><th></th></tr>
    ${rows.map((p) => `<tr><td>${esc(p.proposal_id)}</td><td>${esc(p.trade_a)}</td><td>${esc(p.trade_b)}</td><td>${chip(p.status)}</td>
      <td>${Object.entries(p.verification).map(([k, v]) => chip(k + "=" + v, v === "false" ? "REJECTED" : "")).join("")}</td>
      <td>${p.status === "VERIFIED" ? `<button class="btn" data-confirm="${esc(p.proposal_id)}">Confirm</button>` : ""}</td></tr>`).join("")}</table></div>`;
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
      <span class="muted">${findings.length} findings; dismissed findings stay visible</span></div>
    <div class="table-wrap"><table><tr><th>Kind</th><th>Status</th><th>Episode</th><th>Rules</th><th>Facts</th><th>Summary</th></tr>
    ${findings.map((f) => `<tr><td>${chip(f.kind)}</td><td>${chip(f.status)}</td><td>${esc(f.episode_id)}</td>
      <td>${f.rule_ids.map((r) => chip(r)).join("")}</td>
      <td>${esc(Object.entries(f.facts).map(([k, v]) => k + ": " + v).join(" | "))}</td><td>${esc(f.summary)}</td></tr>`).join("")}</table></div>`;
  $("#run-challenge").onclick = async () => { await act("Challenger", () => api("/api/challenge", { method: "POST", body: "{}" })); challenge(); };
}

// ------------------------------------------------------------------ discovery + governance

async function evolution() {
  const el = $("#view-evolution");
  const [patterns, candidates] = await Promise.all([api("/api/patterns"), api("/api/candidates")]);
  el.innerHTML = `<div class="toolbar"><button class="btn primary" id="run-discover">Discover patterns</button>
      <span class="muted">candidate &rarr; replay &rarr; counterexamples &rarr; shadow &rarr; submit &rarr; four-eyes approval &rarr; versioned release</span></div>
    <h2>Patterns</h2><div class="table-wrap"><table><tr><th>Kind</th><th>Items</th><th>Support</th><th>Escalated</th><th>Cleared</th><th>Rule coverage</th></tr>
      ${patterns.map((p) => `<tr><td>${chip(p.kind)}</td><td>${p.items.map((i) => chip(i)).join("")}</td><td>${esc(p.support)}</td>
        <td>${esc(p.escalated)}</td><td>${esc(p.cleared)}</td><td>${esc(p.rule_coverage)}</td></tr>`).join("")}</table></div>
    <h2>Candidates</h2>
    ${candidates.map((c) => `<div class="card" style="margin-bottom:10px">
      <div class="toolbar"><b>${esc(c.candidate.candidate_id)}</b> ${chip(c.candidate.kind)} ${chip(c.state)}
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
        <h3>Artifacts</h3><pre>${esc(JSON.stringify(d.artifacts, null, 1))}</pre>`;
    };
  });
}

// ------------------------------------------------------------------ policy + audit

async function policy() {
  const el = $("#view-policy");
  const p = await api("/api/policy");
  el.innerHTML = `<h2>Active policy bundle: ${esc(p.active?.bundle_id)}</h2>
    <div class="two"><div class="card"><h3>Detection rules</h3>
      ${(p.active?.ruleset.rules || []).map((r) => `<div>${chip(r.subrule_id)} ${esc(r.description)}<pre>${esc(JSON.stringify(r.conditions))}</pre></div>`).join("")}</div>
      <div class="card"><h3>Bulk-review policy</h3>${(p.active?.bulk_policy.allowed || []).map((s) => chip(s.hypothesis_type + " @ " + s.desk)).join("")}</div></div>
    <h2>Version history</h2><table><tr><th>Bundle</th><th>Parent</th><th>Created by</th><th>From candidate</th><th>Rules</th><th>Bulk scopes</th><th></th></tr>
      ${p.bundles.map((b) => `<tr><td>${esc(b.bundle_id)}</td><td>${esc(b.parent_id || "")}</td><td>${esc(b.created_by)}</td>
        <td>${esc(b.source_candidate || "")}</td><td>${esc(b.rules)}</td><td>${esc(b.bulk_scopes)}</td>
        <td><button class="btn" data-rollback="${esc(b.bundle_id)}">activate</button></td></tr>`).join("")}</table>`;
  el.querySelectorAll("[data-rollback]").forEach((b) => {
    b.onclick = async () => { await act("Activation", () => api("/api/policy/rollback", { method: "POST", body: JSON.stringify({ bundle_id: b.dataset.rollback, reason: "console activation" }) })); policy(); };
  });
}

async function audit() {
  const el = $("#view-audit");
  const a = await api("/api/audit?limit=200");
  el.innerHTML = `<h2>Audit chain ${a.valid ? chip("valid", "VERIFIED") : chip("BROKEN", "REJECTED")} <span class="muted">${esc(a.entries)} entries, hash-linked, append-only</span></h2>
    <div class="table-wrap"><table><tr><th>#</th><th>At</th><th>Actor</th><th>Action</th><th>Subject</th><th>Detail</th><th>Hash</th></tr>
    ${a.recent.map((r) => `<tr><td>${esc(r.seq)}</td><td>${esc(r.at)}</td><td>${esc(r.actor)}</td><td>${esc(r.action)}</td>
      <td>${esc(r.subject)}</td><td>${esc(JSON.stringify(r.detail))}</td><td class="muted">${esc(r.hash.slice(0, 12))}</td></tr>`).join("")}</table></div>`;
}

document.querySelectorAll("#tabs button").forEach((b) => { b.onclick = () => show(b.dataset.view); });
try { const saved = localStorage.getItem("asas.identity"); if (saved) $("#identity").value = saved; } catch { /* storage unavailable */ }
$("#identity").onchange = (e) => { try { localStorage.setItem("asas.identity", e.target.value); } catch { /* ignore */ } };
overview().catch((err) => { $("#view-overview").innerHTML = `<p>Could not load: ${esc(err.message)}</p>`; });

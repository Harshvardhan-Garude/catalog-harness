"use strict";

// This client reads saved predictions and deterministic placement previews.
// It has no model endpoint. Every source/model string is rendered as text.
const view = { state: null, products: [], focus: null, selected: new Set(), preview: null,
  previewSelected: new Set(), accepted: new Set(), busy: false };
const byId = (id) => document.getElementById(id);
const number = (value) => typeof value === "number" ? value.toLocaleString() : "—";
const human = (value) => typeof value === "string" ? value.replaceAll("_", " ") : "Unknown";

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function append(parent, ...children) {
  for (const child of children) if (child) parent.append(child);
  return parent;
}

function jsonDetails(title, value, className = "raw-details") {
  const details = node("details", className);
  append(details, node("summary", "", title), node("pre", "", JSON.stringify(value, null, 2)));
  return details;
}

function announce(message) { byId("live-region").textContent = message; }

function outputOf(product, variant) {
  const saved = product && product[variant];
  return saved && saved.output ? saved.output : null;
}

function decisionsDiffer(product) {
  const baseline = outputOf(product, "baseline"), candidate = outputOf(product, "candidate");
  if (!baseline || !candidate) return Boolean(baseline || candidate);
  return ["category", "primary_use", "secondary_uses"].some((field) =>
    JSON.stringify(baseline[field]) !== JSON.stringify(candidate[field]));
}

function metric(id, before, after) {
  const target = byId(id);
  target.replaceChildren();
  append(target, node("span", "before", before), node("span", "arrow", "→"), node("span", "after", after));
}

function countOf(report, variant, split, field) {
  const run = report.runs && report.runs[variant + "_" + split];
  return run && run.overall ? run.overall[field] : undefined;
}

function fraction(report, variant, split) {
  const numerator = countOf(report, variant, split, "correct_field_decisions");
  const denominator = countOf(report, variant, split, "total_field_decisions");
  return typeof numerator === "number" && typeof denominator === "number" ? `${numerator}/${denominator}` : "—";
}

function renderOverview() {
  const state = view.state, report = state.experiment || {}, source = state.source || {};
  const banner = byId("source-banner");
  banner.className = "source-banner" + (source.kind === "local_fallback" ? " fallback" : "");
  byId("source-icon").textContent = source.kind === "atlas" ? "✓" : "◷";
  byId("source-label").textContent = source.label || "Saved experiment";
  byId("source-notice").textContent = source.notice || "This demo reads saved results and does not perform inference.";
  byId("experiment-state").textContent = source.kind === "atlas" ? "ATLAS READ" : "LOCAL FALLBACK";
  metric("metric-validation", fraction(report, "baseline", "validation"), fraction(report, "candidate", "validation"));
  metric("metric-development", fraction(report, "baseline", "development"), fraction(report, "candidate", "development"));
  metric("metric-unsupported", number(countOf(report, "baseline", "validation", "unsupported_assertions")),
    number(countOf(report, "candidate", "validation", "unsupported_assertions")));
  metric("metric-abstentions", number(countOf(report, "baseline", "validation", "unnecessary_abstentions")),
    number(countOf(report, "candidate", "validation", "unnecessary_abstentions")));
  const before = countOf(report, "baseline", "validation", "correct_field_decisions");
  const after = countOf(report, "candidate", "validation", "correct_field_decisions");
  byId("metric-validation-note").textContent = typeof before === "number" && typeof after === "number"
    ? `${after - before > 0 ? "+" : ""}${after - before} correct decision${Math.abs(after - before) === 1 ? "" : "s"} · synthetic validation only`
    : "Separate data decides acceptance.";
  const decision = report.decision || {};
  const decisionBox = byId("decision");
  decisionBox.replaceChildren();
  append(decisionBox, node("span", "small-dot"), node("strong", "", decision.accepted ? "Candidate accepted." : "Candidate not accepted."),
    node("span", "", decision.reason ? human(decision.reason) + "." : "No saved selection decision."));
  const usage = report.usage || {}, tokens = usage.tokens || {}, proposal = (usage.by_stage || {}).policy_proposal || {};
  byId("usage").replaceChildren();
  append(byId("usage"), node("div", "", `${number(usage.calls)} / ${number(usage.max_calls || 20)} model calls · ${number(tokens.total_tokens)} total tokens`),
    node("div", "", `${number(tokens.input_tokens)} input · ${number(tokens.output_tokens)} output · proposal included (${number(proposal.total_tokens)} tokens)`));
  byId("policy-decision").textContent = decision.accepted ? "Accepted on validation" : "Not accepted on validation";
  renderFinal(state.final_evaluation);
}

function renderFinal(finalReport) {
  const box = byId("final-summary");
  box.hidden = !finalReport;
  box.replaceChildren();
  if (!finalReport) return;
  const report = finalReport.evaluation || finalReport, counts = report.overall || {};
  append(box, node("h3", "", "Final holdout · aggregate results only"));
  if (finalReport.source) append(box, node("p", "", finalReport.source.label));
  if (typeof counts.correct_field_decisions === "number") {
    append(box, node("p", "", `${number(counts.correct_field_decisions)} / ${number(counts.total_field_decisions)} correct field decisions · ${number(counts.unsupported_assertions)} unsupported assertions · ${number(counts.unnecessary_abstentions)} unnecessary abstentions`));
  }
  for (const [stage, run] of Object.entries(report.runs || {})) {
    const aggregate = run.overall || {};
    if (typeof aggregate.correct_field_decisions !== "number") continue;
    append(box, node("p", "", `${human(stage)}: ${number(aggregate.correct_field_decisions)} / ${number(aggregate.total_field_decisions)} correct decisions · ${number(aggregate.unsupported_assertions)} unsupported · ${number(aggregate.unnecessary_abstentions)} unnecessary abstentions`));
  }
  append(box, node("p", "", "Locked policy evaluation. Final products and answer keys are excluded from the inspector."),
    jsonDetails("Aggregate evaluation report", finalReport));
}

function filteredProducts() {
  const query = byId("search").value.trim().toLowerCase(), split = byId("split-filter").value;
  return view.products.filter((product) => {
    const fields = product.fields || {};
    const haystack = [product.source_record_id, fields.name, fields.description, fields.supplier_notes].join(" ").toLowerCase();
    return (!query || haystack.includes(query)) && (split === "all" || product.split === split)
      && (!byId("changed-only").checked || decisionsDiffer(product));
  });
}

function renderProducts() {
  const list = byId("product-list"), scroll = list.scrollTop, products = filteredProducts();
  list.replaceChildren();
  byId("product-count").textContent = `${products.length} / ${view.products.length} listings`;
  if (!products.length) append(list, node("p", "list-empty", "No listings match these filters."));
  for (const product of products) {
    const row = node("div", "product-row" + (product.source_record_id === view.focus ? " selected" : ""));
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.checked = view.selected.has(product.source_record_id);
    checkbox.setAttribute("aria-label", `Select ${product.fields.name} for placement`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) view.selected.add(product.source_record_id); else view.selected.delete(product.source_record_id);
      renderSelection();
    });
    const button = node("button", "product-open");
    button.type = "button";
    button.setAttribute("aria-pressed", product.source_record_id === view.focus ? "true" : "false");
    append(button, node("strong", "", product.fields.name));
    const meta = node("span", "product-meta");
    append(meta, node("span", "split-tag " + product.split, product.split),
      node("span", decisionsDiffer(product) ? "changed" : "", decisionsDiffer(product) ? "↗ Decision changed" : "Saved decision"));
    append(button, meta);
    button.addEventListener("click", () => { view.focus = product.source_record_id; renderProducts(); renderDetail(product); });
    append(row, checkbox, button);
    list.append(row);
  }
  list.scrollTop = scroll;
  renderSelection();
}

function renderSelection() {
  byId("selected-count").textContent = `${view.selected.size} selected`;
  byId("placement-selection").textContent = view.selected.size ? `${view.selected.size} selected listings` : "All saved listings";
  const visible = filteredProducts();
  byId("select-visible").textContent = visible.length && visible.every((product) => view.selected.has(product.source_record_id))
    ? "Clear visible selection" : "Select visible for placement";
}

function renderDetail(product) {
  const target = byId("product-detail");
  target.replaceChildren();
  const top = node("div", "detail-top"), title = node("div", "detail-title"), tags = node("div", "detail-tags");
  append(title, node("h3", "", product.fields.name), node("p", "detail-id mono", product.source_record_id));
  append(tags, node("span", "split-tag " + product.split, product.split));
  if (decisionsDiffer(product)) append(tags, node("span", "count-pill", "Decision changed"));
  append(top, title, tags);
  const raw = node("div", "raw-card");
  append(raw, node("span", "card-caption", "ORIGINAL SUPPLIER FIELDS"));
  for (const field of ["name", "description", "supplier_notes"]) {
    const value = product.fields[field], line = node("div", "source-field");
    append(line, node("span", "", field), node("span", value ? "" : "empty-value", value || "Not provided"));
    append(raw, line);
  }
  append(raw, jsonDetails("Raw supplier payload & normalized product", {raw_payload: product.raw_payload || {}, normalized_product: product.normalized_product || {}}));
  const comparison = node("div", "result-comparison");
  append(comparison, resultCard(product, "baseline"), resultCard(product, "candidate"));
  append(target, top, raw, comparison);
}

function resultCard(product, variant) {
  const card = node("article", "result-card " + variant), output = outputOf(product, variant), saved = product[variant] || {};
  const heading = node("div", "result-header");
  append(heading, node("h4", "", variant === "baseline" ? "Baseline" : "Candidate"),
    node("span", "", variant === "baseline" ? "Strong initial policy" : "Revised harness policy"));
  card.append(heading);
  if (!output) { card.append(node("p", "muted", "No saved output for this variant.")); return card; }
  for (const [field, title] of [["category", "Category"], ["primary_use", "Primary use"], ["secondary_uses", "Secondary uses"]]) {
    card.append(node("span", "result-label", title));
    const value = node("div", "result-value");
    if (field === "secondary_uses") {
      const uses = Array.isArray(output[field]) ? output[field] : [];
      if (!uses.length) value.append(node("span", "no-secondary", "None asserted"));
      for (const use of uses) value.append(node("span", "label-chip", human(use)));
    } else {
      value.append(node("span", "label-chip" + (output[field] === "unknown" ? " unknown" : ""), human(output[field])));
    }
    card.append(value);
  }
  card.append(node("div", "citation-heading", "Supporting source evidence"));
  const evidence = Array.isArray(output.evidence) ? output.evidence : [];
  if (!evidence.length) card.append(node("p", "no-secondary", "No label citations; unknown decisions require review."));
  for (const citation of evidence) {
    const line = node("div", "citation");
    append(line, node("div", "citation-meta", `${human(citation.field)} · ${human(citation.label)} · ${citation.source_field}`),
      node("blockquote", "", `“${citation.excerpt}”`));
    card.append(line);
  }
  for (const review of Array.isArray(output.review) ? output.review : []) {
    const item = node("div", "review-item");
    append(item, node("strong", "", `${human(review.field)} needs review. `), document.createTextNode(String(review.reason || "")));
    card.append(item);
  }
  const checks = Array.isArray(saved.evidence_checks) ? saved.evidence_checks : [];
  const original = saved.original_model_output || output;
  card.append(jsonDetails(`Original model response & checks (${checks.length})`, {
    original_model_output: original, persisted_output: output, evidence_checks: checks,
    original_evidence_checks: saved.original_evidence_checks || []
  }, "mechanical-details"));
  return card;
}

function selectedProfile() {
  return ((view.state && view.state.profiles) || []).find((profile) => profile.store_id === byId("store-profile").value);
}

function renderProfiles(previous) {
  const select = byId("store-profile");
  select.replaceChildren();
  for (const profile of view.state.profiles || []) {
    const option = node("option", "", profile.name || profile.store_id);
    option.value = profile.store_id;
    select.append(option);
  }
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  renderProfile();
}

function renderProfile() {
  const profile = selectedProfile(), summary = byId("profile-summary");
  summary.replaceChildren();
  if (!profile) { append(summary, node("span", "", "No store profiles are available.")); byId("preview-button").disabled = true; return; }
  const description = node("span");
  append(description, node("strong", "", profile.name + " · "), document.createTextNode(profile.description || ""));
  append(summary, description, node("span", "", `${(profile.rules || []).length} rules · ${(profile.allowed_paths || []).length} allowed paths · ${profile.version || ""}`));
  byId("profile-json").textContent = JSON.stringify(profile, null, 2);
  byId("preview-button").disabled = view.busy;
}

function placementStatus(message, error = false) {
  const status = byId("placement-status");
  status.hidden = !message;
  status.textContent = message || "";
  status.className = "inline-notice" + (error ? " error" : "");
  if (message) announce(message);
}

function clearPreview() {
  view.preview = null;
  view.previewSelected.clear();
  view.accepted.clear();
  byId("preview-panel").hidden = true;
  byId("preview-panel").replaceChildren();
  placementStatus("");
}

function renderPreview() {
  const box = byId("preview-panel"), preview = view.preview;
  box.replaceChildren();
  box.hidden = !preview;
  if (!preview) return;
  const records = Array.isArray(preview.records) ? preview.records : [], counts = preview.counts || {};
  const header = node("div", "preview-header"), heading = node("div");
  append(heading, node("h3", "", "Staged placement preview"),
    node("p", "", `${number(counts.selected || records.length)} selected · ${number(counts.ready)} ready · ${number(counts.needs_review)} need review · ${number(counts.unmapped)} unmapped`));
  const atlasAvailable = view.state.source && view.state.source.kind === "atlas";
  const accept = node("button", "button dark", atlasAvailable
    ? `Accept ${view.previewSelected.size} ready placement${view.previewSelected.size === 1 ? "" : "s"}`
    : "Atlas required to accept");
  accept.type = "button";
  accept.disabled = view.busy || !view.previewSelected.size || !atlasAvailable;
  if (!atlasAvailable) accept.title = "Local fallback supports previews. Atlas must be available to persist placements.";
  accept.addEventListener("click", acceptPreview);
  append(header, heading, accept);
  const wrap = node("div", "preview-table-wrap"), table = node("table", "preview-table"), head = node("thead"), headerRow = node("tr");
  for (const label of ["", "Source product", "Store category", "Status", "Reason"]) headerRow.append(node("th", "", label));
  head.append(headerRow);
  const body = node("tbody");
  for (const record of records) {
    const row = node("tr"), choice = node("td"), checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.checked = view.previewSelected.has(record.source_record_id);
    checkbox.disabled = record.status !== "ready" || view.accepted.has(record.source_record_id) || view.busy;
    checkbox.setAttribute("aria-label", `Accept placement for ${record.name || record.source_record_id}`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) view.previewSelected.add(record.source_record_id); else view.previewSelected.delete(record.source_record_id);
      renderPreview();
    });
    choice.append(checkbox);
    const product = node("td", "", record.name || record.source_record_id);
    product.append(node("small", "mono", record.source_record_id));
    const status = node("td"), accepted = view.accepted.has(record.source_record_id);
    status.append(node("span", "status-chip" + (record.status === "ready" ? "" : " review"), accepted ? "accepted" : human(record.status)));
    append(row, choice, product, node("td", "", record.store_category || "Awaiting review"), status,
      node("td", "", Array.isArray(record.reasons) ? record.reasons.join(" · ") : ""));
    body.append(row);
  }
  append(table, head, body);
  wrap.append(table);
  append(box, header, wrap, node("div", "preview-footer", preview.notice || "Acceptance persists store placements separately. Original supplier records and measured experiment results remain unchanged."));
}

function renderPolicies() {
  const target = byId("policy-comparison"), policies = view.state.policies || (view.state.experiment || {}).policies || {};
  target.replaceChildren();
  for (const variant of ["baseline", "candidate"]) {
    const policy = policies[variant], card = node("article", "policy-card " + variant);
    append(card, node("h3", "", variant === "baseline" ? "Baseline policy" : "Candidate policy"));
    if (!policy) { card.append(node("p", "muted", "No saved policy available.")); target.append(card); continue; }
    append(card, node("p", "policy-version", policy.version), node("span", "result-label", "Review behavior"),
      node("p", "policy-behavior", policy.review_behavior), node("span", "result-label", "Exact instruction addendum"),
      node("p", "policy-addendum", policy.instruction_addendum || "No additional instructions. The full baseline instructions apply."));
    const full = (view.state.policy_instructions || {})[variant] || policy.instructions || policy.base_instructions;
    if (full) {
      const details = node("details");
      append(details, node("summary", "", "Exact full model instructions"), node("pre", "", full));
      card.append(details);
    }
    card.append(jsonDetails("Saved policy JSON", policy));
    target.append(card);
  }
}

async function api(path, body) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 40000);
  try {
    const options = {method: body === undefined ? "GET" : "POST", signal: controller.signal, credentials: "same-origin", headers: {"Accept": "application/json"}};
    if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    const response = await fetch(path, options);
    let result;
    try { result = await response.json(); } catch (_) { throw new Error("invalid_server_response"); }
    if (!response.ok) {
      const error = result && result.error, code = typeof error === "string" ? error : error && error.code;
      const failure = new Error(typeof code === "string" && /^[a-z0-9_]{1,80}$/.test(code) ? code : `http_${response.status}`);
      // These are the server's fixed, sanitized help messages, never driver
      // exceptions. Render them only as text; no headers or config are shown.
      if (typeof error === "string" && error.length <= 1500) failure.userMessage = error;
      throw failure;
    }
    return result;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("request_timeout");
    if (error instanceof TypeError) throw new Error("local_server_unavailable");
    throw error;
  } finally { clearTimeout(timer); }
}

async function refresh() {
  const button = byId("refresh");
  button.disabled = true;
  try {
    const previousProfile = byId("store-profile").value;
    const state = await api("/api/state");
    if (!state || !Array.isArray(state.products)) throw new Error("invalid_saved_state");
    view.state = state;
    // Final holdout has no product inspection route, even if a server adds it.
    view.products = state.products.filter((product) => ["development", "validation"].includes(product.split));
    view.selected = new Set([...view.selected].filter((id) => view.products.some((product) => product.source_record_id === id)));
    if (!view.products.some((product) => product.source_record_id === view.focus)) {
      const runs = (state.experiment || {}).runs || {};
      const baselineFailures = new Set(((runs.baseline_validation || {}).representative_failures || []).map((row) => row.source_record_id));
      const candidateFailures = new Set(((runs.candidate_validation || {}).representative_failures || []).map((row) => row.source_record_id));
      const first = view.products.find((product) => product.split === "validation" && decisionsDiffer(product)
        && baselineFailures.has(product.source_record_id) && !candidateFailures.has(product.source_record_id))
        || view.products.find((product) => product.split === "validation" && decisionsDiffer(product)) || view.products[0];
      view.focus = first ? first.source_record_id : null;
    }
    renderOverview(); renderProducts(); renderProfiles(previousProfile); renderPolicies(); clearPreview();
    const focused = view.products.find((product) => product.source_record_id === view.focus);
    if (focused) renderDetail(focused);
    announce(`Loaded ${view.products.length} saved product listings. No model calls.`);
  } catch (error) {
    byId("source-banner").className = "source-banner error";
    byId("source-icon").textContent = "!";
    byId("source-label").textContent = "Saved demo data unavailable";
    const local = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
    const advice = error.userMessage || (local ? "Start the local demo server and refresh."
      : "The deployed service cannot load saved results. Check its hosting configuration and Atlas access, then refresh.");
    byId("source-notice").textContent = `${advice} Error category: ${error.message}. No automatic retries.`;
    byId("experiment-state").textContent = "UNAVAILABLE";
  } finally { button.disabled = false; }
}

async function previewPlacements() {
  if (view.busy || !selectedProfile()) return;
  view.busy = true;
  byId("preview-button").disabled = true;
  placementStatus("Preparing deterministic placement preview…");
  try {
    const body = {store_id: selectedProfile().store_id};
    if (view.selected.size) body.source_record_ids = [...view.selected];
    const preview = await api("/api/placement/preview", body);
    if (!preview || !Array.isArray(preview.records) || !preview.preview_id) throw new Error("invalid_preview");
    view.preview = preview;
    view.accepted.clear();
    view.previewSelected = new Set(preview.records.filter((record) => record.status === "ready").map((record) => record.source_record_id));
    placementStatus("Preview staged. Review the store paths below before accepting ready placements. No model calls.");
  } catch (error) { placementStatus(`Preview failed: ${error.userMessage || human(error.message)}.`, true); }
  finally { view.busy = false; byId("preview-button").disabled = false; renderPreview(); }
}

async function acceptPreview() {
  if (view.busy || !view.preview || !view.previewSelected.size) return;
  view.busy = true;
  renderPreview();
  try {
    const result = await api("/api/placement/accept", {preview_id: view.preview.preview_id, source_record_ids: [...view.previewSelected]});
    for (const id of result.source_record_ids || []) { view.accepted.add(id); view.previewSelected.delete(id); }
    placementStatus(`Accepted ${number(result.accepted_count)} placement${result.accepted_count === 1 ? "" : "s"} for ${result.store_id}. ${result.notice || "Stored separately from supplier records and experiment results."}`);
  } catch (error) { placementStatus(`Acceptance failed: ${error.userMessage || human(error.message)}.`, true); }
  finally { view.busy = false; renderPreview(); }
}

byId("refresh").addEventListener("click", refresh);
byId("search").addEventListener("input", renderProducts);
byId("split-filter").addEventListener("change", renderProducts);
byId("changed-only").addEventListener("change", renderProducts);
byId("select-visible").addEventListener("click", () => {
  const products = filteredProducts(), allSelected = products.length && products.every((product) => view.selected.has(product.source_record_id));
  for (const product of products) { if (allSelected) view.selected.delete(product.source_record_id); else view.selected.add(product.source_record_id); }
  renderProducts();
});
byId("store-profile").addEventListener("change", () => { renderProfile(); clearPreview(); });
byId("preview-button").addEventListener("click", previewPlacements);
for (const link of document.querySelectorAll(".nav-item")) link.addEventListener("click", () => {
  for (const other of document.querySelectorAll(".nav-item")) other.classList.toggle("active", other === link);
});
refresh();

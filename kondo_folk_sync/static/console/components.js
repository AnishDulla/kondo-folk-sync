export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

export function groupOption(value, label, selected) {
  return `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`;
}

export function depthPill(row) {
  if (row.sync_depth === "full_history") return `<span class="pill green">Full history</span>`;
  if (row.needs_full_history) return `<span class="pill amber">Latest only - full recommended</span>`;
  return `<span class="pill">Latest message</span>`;
}

export function statusPill(row) {
  const cls = row.console_state === "selected" ? "green" :
    row.console_state === "waiting" ? "amber" :
    row.console_state === "sent" ? "blue" :
    row.console_state === "skipped" ? "red" : "";
  return `<span class="pill ${cls}">${escapeHtml(row.console_label)}</span>`;
}

export function actionButtons(row) {
  const key = encodeURIComponent(row.idempotency_key);
  const actions = [];
  if (row.console_state === "review" || row.console_state === "full_ready") {
    actions.push(`<button class="green-btn" data-post="/stage/${key}">${row.sync_depth === "full_history" ? "Select Full" : "Select Latest"}</button>`);
    if (row.sync_depth !== "full_history") actions.push(`<button class="amber-btn" data-post="/request-full-sync/${key}">Request Full</button>`);
    actions.push(`<button class="ghost" data-post="/skip/${key}">Skip</button>`);
  } else if (row.console_state === "selected") {
    actions.push(`<button class="ghost" data-post="/unstage/${key}">Remove</button>`);
    if (row.sync_depth !== "full_history") actions.push(`<button class="amber-btn" data-post="/request-full-sync/${key}">Request Full</button>`);
  } else if (row.console_state === "waiting") {
    if (row.kondo_url) actions.push(`<a class="button-link amber-btn" href="${escapeHtml(row.kondo_url)}" target="_blank" rel="noreferrer">Open Kondo Full Sync</a>`);
    actions.push(`<button class="ghost" data-post="/stage/${key}">Use Latest Anyway</button>`);
  } else if (row.console_state === "sent") {
    actions.push(`<button class="ghost" data-post="/stage/${key}">Select to Resend</button>`);
  } else if (row.console_state === "skipped") {
    actions.push(`<button class="ghost" data-relevant="${key}">Mark Relevant</button>`);
  }
  if (row.linkedin_url) actions.push(`<a class="button-link ghost" href="${escapeHtml(row.linkedin_url)}" target="_blank" rel="noreferrer">LinkedIn</a>`);
  return actions.join("");
}

export function cardClass(row, previousRows) {
  const classes = ["contact-card", row.console_state];
  if (row.console_state === "selected") classes.push("selected");
  if (row.console_state === "waiting") classes.push("waiting");
  const previous = previousRows.get(row.idempotency_key);
  if (previous && previous.state !== row.console_state + row.sync_depth + row.updated_at) classes.push("updated");
  return classes.join(" ");
}

export function renderCard(row, previousRows) {
  const labels = (row.labels || []).slice(0, 3).map((label) => `<span class="pill">${escapeHtml(label)}</span>`).join("");
  const reasons = (row.reasons || []).slice(0, 5).map((reason) => `<span class="pill">${escapeHtml(reason)}</span>`).join("");
  const latest = row.latest_message ? escapeHtml(row.latest_message).slice(0, 260) : "No latest message captured.";
  const summary = row.summary ? escapeHtml(row.summary).slice(0, 260) : "No AI summary yet.";
  const meta = [row.headline, row.company, row.conversation_time].filter(Boolean).map(escapeHtml).join(" · ");
  return `<article class="${cardClass(row, previousRows)}" data-key="${escapeHtml(row.idempotency_key)}">
    <div class="card-head">
      <div>
        <div class="name-line">
          <span class="contact-name">${escapeHtml(row.full_name || "Unknown contact")}</span>
          <span class="pill">score ${escapeHtml(row.score ?? 0)}</span>
        </div>
        <div class="meta">${meta}</div>
      </div>
      <div class="pills">
        ${statusPill(row)}
        ${depthPill(row)}
        <span class="pill blue">${escapeHtml(row.group_category || "uncategorized")}</span>
      </div>
    </div>
    <div class="card-body">
      <div>
        <div class="label">AI Readout</div>
        <div class="body-text">${summary}</div>
        <div class="evidence">${reasons || labels || "<span class='muted'>No evidence tags.</span>"}</div>
        <div class="small">${escapeHtml(row.relationship_stage || "")} · ${escapeHtml(row.reply_owner || "")} · confidence ${escapeHtml(row.confidence ?? 0)}</div>
      </div>
      <div>
        <div class="label">What happened</div>
        <div class="body-text">${latest}</div>
        <div class="small">Next: ${escapeHtml(row.next_action || "Review conversation.")}</div>
      </div>
      <div>
        <div class="label">Decision</div>
        <div class="actions">${actionButtons(row)}</div>
        <div class="bucket-row">
          <select data-group="${escapeHtml(row.idempotency_key)}">
            ${groupOption("claims_professionals", "Claims professional", row.group_category)}
            ${groupOption("distribution_partners", "Distribution partner", row.group_category)}
            ${groupOption("tpas_subrogation_attorneys", "TPA / subro attorney", row.group_category)}
          </select>
        </div>
      </div>
    </div>
  </article>`;
}

export function renderMetrics(summary) {
  const cards = [
    ["Needs review", summary.needs_review || 0],
    ["Selected", summary.selected || 0],
    ["Full selected", summary.selected_full || 0],
    ["Waiting full", summary.waiting || 0],
    ["Queue", summary.queue_depth || 0],
  ];
  return cards.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
}

export function renderBatch(rows, summary) {
  const selected = rows.filter((row) => row.console_state === "selected");
  if (!selected.length) {
    return `<div class="batch-row"><span class="muted">Select contacts from the review list. folk will not be touched until you send this batch.</span></div>`;
  }
  return selected.map((row) => `<div class="batch-row">
    <div>
      <strong>${escapeHtml(row.full_name || "Unknown contact")}</strong>
      <span class="muted">${row.sync_depth === "full_history" ? "Full conversation" : "Latest message"}</span>
    </div>
    <button class="ghost" data-post="/unstage/${encodeURIComponent(row.idempotency_key)}">Remove</button>
  </div>`).join("");
}

export function batchSummary(rows, summary) {
  const selected = rows.filter((row) => row.console_state === "selected");
  return selected.length
    ? `${selected.length} selected: ${summary.selected_latest || 0} latest, ${summary.selected_full || 0} full`
    : "Nothing selected.";
}

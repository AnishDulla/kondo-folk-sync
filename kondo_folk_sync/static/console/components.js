export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function truncate(value, length = 220) {
  const text = String(value ?? "").trim();
  return text.length > length ? `${text.slice(0, length - 1)}...` : text;
}

function groupOption(value, label, selected) {
  return `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`;
}

function stateCopy(row) {
  if (row.console_state === "selected") return "Selected for folk";
  if (row.console_state === "waiting") return "Waiting for Kondo full sync";
  if (row.console_state === "sent") return "Already sent to folk";
  if (row.console_state === "skipped") return "Skipped";
  if (row.sync_depth === "full_history") return "Full history ready";
  return "Needs decision";
}

function depthCopy(row) {
  if (row.sync_depth === "full_history") return "Full conversation";
  if (row.needs_full_history) return "Latest only - full recommended";
  return "Latest message";
}

function latestMessageLabel(row) {
  if (row.latest_message_direction === "user") return "Latest message from you";
  if (row.latest_message_direction === "prospect") return "Latest message from prospect";
  return "Latest message, sender unknown";
}

export function statusPill(row) {
  const cls = row.console_state === "selected" ? "green" :
    row.console_state === "waiting" ? "amber" :
    row.console_state === "sent" ? "blue" :
    row.console_state === "skipped" ? "red" :
    row.sync_depth === "full_history" ? "green" : "";
  return `<span class="pill ${cls}">${escapeHtml(stateCopy(row))}</span>`;
}

export function depthPill(row) {
  const cls = row.sync_depth === "full_history" ? "green" : row.needs_full_history ? "amber" : "";
  return `<span class="pill ${cls}">${escapeHtml(depthCopy(row))}</span>`;
}

export function actionButtons(row) {
  const key = encodeURIComponent(row.idempotency_key);
  const actions = [];
  if (row.console_state === "review" || row.console_state === "full_ready") {
    actions.push(`<button class="green-btn" data-post="/stage/${key}">${row.sync_depth === "full_history" ? "Select Full" : "Select Latest"}</button>`);
    if (row.sync_depth !== "full_history") actions.push(`<button class="amber-btn" data-post="/request-full-sync/${key}">Request Full History</button>`);
    actions.push(`<button class="ghost" data-post="/skip/${key}">Skip</button>`);
  } else if (row.console_state === "selected") {
    actions.push(`<button class="ghost" data-post="/unstage/${key}">Remove from Batch</button>`);
    if (row.sync_depth !== "full_history") actions.push(`<button class="amber-btn" data-post="/request-full-sync/${key}">Request Full History</button>`);
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

export function rowClass(row, previousRows) {
  const classes = ["triage-row", row.console_state];
  if (row.sync_depth === "full_history") classes.push("full-history");
  const previous = previousRows.get(row.idempotency_key);
  if (previous && previous.state !== row.console_state + row.sync_depth + row.updated_at) classes.push("updated");
  return classes.join(" ");
}

export function renderTriageRow(row, previousRows) {
  const latest = truncate(row.latest_message || "No latest message captured.", 260);
  const summary = truncate(row.summary || "No AI summary yet.", 220);
  const nextAction = truncate(row.next_action || "Review conversation.", 150);
  const reasons = (row.reasons || row.labels || []).slice(0, 3).map((reason) => `<span class="evidence-chip">${escapeHtml(reason)}</span>`).join("");
  const profile = [row.headline, row.company].filter(Boolean).map(escapeHtml).join(" · ");
  return `<article class="${rowClass(row, previousRows)}" data-key="${escapeHtml(row.idempotency_key)}">
    <section class="person-cell">
      <div class="contact-name">${escapeHtml(row.full_name || "Unknown contact")}</div>
      <div class="meta">${profile || "No profile details captured."}</div>
      <div class="meta">${escapeHtml(row.conversation_time || "No conversation timestamp")}</div>
    </section>
    <section class="message-cell">
      <div class="cell-label">${latestMessageLabel(row)}</div>
      <div class="body-text">${escapeHtml(latest)}</div>
      ${row.conversation_status ? `<div class="meta">Kondo status: ${escapeHtml(row.conversation_status)}</div>` : ""}
    </section>
    <section class="ai-cell">
      <div class="cell-label">AI readout</div>
      <div class="body-text">${escapeHtml(summary)}</div>
      <div class="next-step">Next: ${escapeHtml(nextAction)}</div>
      <div class="evidence">${reasons || "<span class='muted'>No evidence tags.</span>"}</div>
    </section>
    <section class="decision-cell">
      <div class="pills">
        ${statusPill(row)}
        ${depthPill(row)}
        <span class="pill blue">${escapeHtml(row.group_category || "uncategorized")}</span>
      </div>
      <select data-group="${escapeHtml(row.idempotency_key)}" aria-label="CRM bucket">
        ${groupOption("claims_professionals", "Claims professional", row.group_category)}
        ${groupOption("distribution_partners", "Distribution partner", row.group_category)}
        ${groupOption("tpas_subrogation_attorneys", "TPA / subro attorney", row.group_category)}
      </select>
      <div class="actions">${actionButtons(row)}</div>
    </section>
  </article>`;
}

export function renderCard(row, previousRows) {
  return renderTriageRow(row, previousRows);
}

export function renderWorkflow(summary) {
  const selected = summary.selected || 0;
  const selectedLatest = summary.selected_latest || 0;
  const selectedFull = summary.selected_full || 0;
  const steps = [
    ["1", "Review contacts", summary.needs_review || 0, "Decide who belongs in folk."],
    ["2", "Choose depth", summary.waiting || 0, "Request full history when context matters."],
    ["3", "Send batch", selected, `${selectedLatest} latest, ${selectedFull} full selected.`],
  ];
  return steps.map(([number, title, count, copy]) => `<div class="workflow-step">
    <span class="step-number">${number}</span>
    <div>
      <strong>${title}</strong>
      <span>${count}</span>
      <p>${copy}</p>
    </div>
  </div>`).join("");
}

export function renderSyncStatus(state) {
  const summary = state.summary || {};
  const counts = state.status_counts || {};
  const queueDepth = summary.queue_depth || 0;
  const ready = summary.needs_review || 0;
  const selected = summary.selected || 0;
  const skipped = summary.skipped || 0;
  const sent = summary.sent || 0;
  const queued = (counts.queued || 0) + (counts.retry_wait || 0) + (counts.error || 0);
  const processing = counts.processing || 0;
  const hasAnyActivity = Boolean(state.last_event_at || ready || selected || skipped || sent || queueDepth);
  let tone = "idle";
  let title = "No Kondo activity yet";
  let copy = "Run a Kondo sync and contacts will appear here as they are received and analyzed.";

  if (queueDepth > 0 || processing > 0 || queued > 0) {
    tone = "working";
    title = "Processing AI triage";
    copy = `${queueDepth} still in queue. ${ready} ready to review so far.`;
  } else if (ready > 0 || selected > 0) {
    tone = "ready";
    title = "Ready for review";
    copy = `${ready} contacts ready. Queue clear.`;
  } else if (hasAnyActivity) {
    tone = "done";
    title = "Sync processed";
    copy = "Queue clear. Check skipped or sent contacts if the open list is empty.";
  }

  const lastUpdate = state.last_event_at ? escapeHtml(state.last_event_at) : "No events received";
  return `<div class="sync-card ${tone}">
    <div>
      <div class="sync-eyebrow">Sync Status</div>
      <h2>${title}</h2>
      <p>${escapeHtml(copy)}</p>
    </div>
    <div class="sync-metrics">
      <span><strong>${queueDepth}</strong> queue</span>
      <span><strong>${ready}</strong> ready</span>
      <span><strong>${selected}</strong> selected</span>
      <span><strong>${skipped}</strong> skipped</span>
      <span><strong>${sent}</strong> sent</span>
    </div>
    <div class="sync-last">Last update: ${lastUpdate}</div>
  </div>`;
}

export function renderBatch(rows, summary) {
  const selected = rows.filter((row) => row.console_state === "selected");
  if (!selected.length) {
    return `<div class="batch-empty">Select contacts from the review list. Nothing goes to folk until you send the batch.</div>`;
  }
  return selected.map((row) => `<div class="batch-row">
    <div>
      <strong>${escapeHtml(row.full_name || "Unknown contact")}</strong>
      <span>${row.sync_depth === "full_history" ? "Full conversation" : "Latest message"}</span>
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

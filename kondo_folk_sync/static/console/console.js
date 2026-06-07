import { createConsoleApi } from "./api.js";
import { batchSummary, renderBatch, renderSyncStatus, renderTriageRow, renderWorkflow } from "./components.js";

const config = window.KONDO_CONSOLE_CONFIG || {};
const api = createConsoleApi(config);

let currentState = null;
let activeFilter = "open";
let searchTerm = "";
let previousRows = new Map();

const workflowEl = document.getElementById("workflow");
const syncStatusEl = document.getElementById("sync-status");
const reviewListEl = document.getElementById("review-list");
const batchListEl = document.getElementById("batch-list");
const batchSummaryEl = document.getElementById("batch-summary");
const sendBatchBtn = document.getElementById("send-batch");
const selectVisibleBtn = document.getElementById("select-visible");
const lastUpdatedEl = document.getElementById("last-updated");
const noticeEl = document.getElementById("notice");
const toastsEl = document.getElementById("toasts");

function toast(message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  toastsEl.appendChild(el);
  setTimeout(() => el.remove(), 5200);
}

async function postAction(path, body = null) {
  await api.post(path, body);
  await loadState(true);
}

function sortRows(rows) {
  return [...rows].sort((a, b) => {
    const aTime = a.conversation_time || a.updated_at || "";
    const bTime = b.conversation_time || b.updated_at || "";
    return String(bTime).localeCompare(String(aTime));
  });
}

function stateMatches(row) {
  if (activeFilter === "open") return ["review", "full_ready", "waiting"].includes(row.console_state);
  if (activeFilter === "full_ready") return row.sync_depth === "full_history" && row.console_state !== "sent";
  return row.console_state === activeFilter;
}

function rowMatches(row) {
  if (!stateMatches(row)) return false;
  if (!searchTerm) return true;
  const haystack = [
    row.full_name,
    row.company,
    row.headline,
    row.linkedin_url,
    row.group_category,
    row.latest_message,
    row.summary,
    row.next_action,
  ].join(" ").toLowerCase();
  return haystack.includes(searchTerm);
}

function emptyMessage() {
  if (activeFilter === "open" && !searchTerm) {
    return "No open contacts. When Kondo sends new conversations, they will appear here for review.";
  }
  if (activeFilter === "selected") return "No selected contacts yet. Choose Latest or Full from the review list.";
  if (activeFilter === "waiting") return "No contacts are waiting for full history.";
  if (activeFilter === "full_ready") return "No full-history contacts are ready yet.";
  return "No contacts match this view.";
}

function renderState(state) {
  currentState = state;
  const rows = sortRows(state.rows || []);
  const summary = state.summary || {};
  const selected = rows.filter((row) => row.console_state === "selected");

  syncStatusEl.innerHTML = renderSyncStatus(state);
  workflowEl.innerHTML = renderWorkflow(summary);
  batchListEl.innerHTML = renderBatch(rows, summary);
  batchSummaryEl.textContent = batchSummary(rows, summary);
  sendBatchBtn.disabled = selected.length === 0;
  sendBatchBtn.textContent = selected.length ? `Send ${selected.length} selected to folk` : "Send selected to folk";
  selectVisibleBtn.disabled = !rows.some((row) => rowMatches(row) && ["review", "full_ready"].includes(row.console_state));
  lastUpdatedEl.textContent = state.last_event_at ? `Most recent Kondo update: ${state.last_event_at}` : "Waiting for Kondo conversations.";

  const visibleRows = rows.filter(rowMatches);
  reviewListEl.innerHTML = visibleRows.length
    ? visibleRows.map((row) => renderTriageRow(row, previousRows)).join("")
    : `<section class="empty-state">${emptyMessage()}</section>`;

  const nextPrevious = new Map();
  for (const row of rows) {
    nextPrevious.set(row.idempotency_key, { state: row.console_state + row.sync_depth + row.updated_at });
  }
  previousRows = nextPrevious;
}

async function loadState(showToast = false) {
  const nextState = await api.getState();
  if (currentState && currentState.revision !== nextState.revision) {
    const oldRows = new Map((currentState.rows || []).map((row) => [row.idempotency_key, row]));
    for (const row of nextState.rows || []) {
      const old = oldRows.get(row.idempotency_key);
      if (old && old.sync_depth !== "full_history" && row.sync_depth === "full_history") toast(`Full history ready: ${row.full_name || "contact"}`);
      else if (!old && row.console_state !== "skipped") toast(`New Kondo conversation: ${row.full_name || "contact"}`);
      else if (old && old.console_state === "waiting" && row.console_state === "full_ready") toast(`Ready to select full history: ${row.full_name || "contact"}`);
    }
  } else if (showToast) {
    toast("Console updated.");
  }
  renderState(nextState);
}

function setActiveFilter(filter) {
  activeFilter = filter;
  document.querySelectorAll("[data-filter]").forEach((button) => {
    button.classList.toggle("active", button.dataset.filter === filter);
  });
  renderState(currentState);
}

document.addEventListener("click", async (event) => {
  const target = event.target.closest("button, a");
  if (!target) return;
  if (target.dataset.filter) {
    event.preventDefault();
    setActiveFilter(target.dataset.filter);
    return;
  }
  if (target.dataset.action) {
    event.preventDefault();
    try { await postAction(target.dataset.action); toast("Admin action started."); } catch (error) { toast(error.message); }
    return;
  }
  if (target.dataset.post) {
    event.preventDefault();
    try { await postAction(target.dataset.post); toast("Selection updated."); } catch (error) { toast(error.message); }
    return;
  }
  if (target.dataset.relevant) {
    event.preventDefault();
    const body = new FormData();
    const row = currentState.rows.find((item) => item.idempotency_key === decodeURIComponent(target.dataset.relevant));
    body.append("group_category", row?.group_category || "claims_professionals");
    try { await postAction(`/mark-relevant/${target.dataset.relevant}`, body); toast("Marked relevant."); } catch (error) { toast(error.message); }
  }
});

document.getElementById("search").addEventListener("input", (event) => {
  searchTerm = event.target.value.toLowerCase().trim();
  renderState(currentState);
});

document.addEventListener("change", async (event) => {
  const target = event.target;
  if (!target.dataset || !target.dataset.group) return;
  const body = new FormData();
  body.append("group_category", target.value);
  try { await postAction(`/group/${encodeURIComponent(target.dataset.group)}`, body); toast("Bucket updated."); } catch (error) { toast(error.message); }
});

sendBatchBtn.addEventListener("click", async () => {
  try { await postAction("/send-staged"); toast("Selected batch queued for folk."); } catch (error) { toast(error.message); }
});

selectVisibleBtn.addEventListener("click", async () => {
  if (!currentState) return;
  const visible = sortRows(currentState.rows || []).filter(rowMatches).filter((row) => row.console_state === "review" || row.console_state === "full_ready");
  for (const row of visible) await postAction(`/stage/${encodeURIComponent(row.idempotency_key)}`);
  toast(`Selected ${visible.length} visible contact(s).`);
});

document.getElementById("reset-state").addEventListener("click", async () => {
  const body = new FormData();
  body.append("confirm", document.getElementById("reset-confirm").value);
  try { await postAction("/reset-local-state", body); toast("Local sync state cleared."); } catch (error) { toast(error.message); }
});

if (config.notice) {
  noticeEl.textContent = config.notice;
  noticeEl.classList.remove("hidden");
}

loadState().catch((error) => toast(error.message));
setInterval(() => loadState().catch((error) => toast(error.message)), 3000);

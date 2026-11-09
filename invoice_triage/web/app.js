// Invoice check: page logic. Vanilla JS, no build step, no external requests.
// All text that comes from documents is inserted with textContent, never innerHTML.
"use strict";

const TOKEN = document.querySelector('meta[name="app-token"]').content;
const $ = (id) => document.getElementById(id);
const POLL_MS = 700;

const view = { state: null, filter: "review", expanded: new Set(), polling: false };

// ---------- helpers ----------

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "X-Token": TOKEN, ...(options.body && typeof options.body === "string"
      ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `The app didn't respond (${res.status}).`);
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) node.append(c);
  return node;
}

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
function money(amount, currency = "USD") {
  if (amount === null || amount === undefined) return "";
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(amount);
  } catch {
    return `${currency} ${amount.toFixed(2)}`;
  }
}

function longDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

function runUrl(runId, what, extra = "") {
  return `/api/runs/${encodeURIComponent(runId)}/${what}?t=${encodeURIComponent(TOKEN)}${extra}`;
}

function notice(title, detail) {
  const box = $("notice");
  box.replaceChildren(el("strong", { text: title }), detail ? el("span", { text: detail }) : null);
  box.hidden = false;
}
function clearNotice() { $("notice").hidden = true; }

// ---------- rendering ----------

function render(state) {
  view.state = state;
  $("thresholdText").textContent = usd.format(state.threshold).replace(".00", "");
  if (!$("driveLink").value && state.drive_link) $("driveLink").value = state.drive_link;

  const claude = state.claude;
  const running = state.run && !["done", "failed"].includes(state.run.stage);
  $("runButton").disabled = !claude.ok || running;
  $("filePick").disabled = !claude.ok || running;
  if (!claude.ok) notice(claude.message, claude.fix);

  renderSchedule(state.schedule);
  renderProgress(state.run);
  if (state.latest && !running) renderResults(state.latest);
}

function renderSchedule(schedule) {
  const toggle = $("scheduleToggle");
  toggle.checked = schedule.enabled;
  toggle.setAttribute("aria-checked", String(schedule.enabled));

  const line = $("scheduleLine");
  line.replaceChildren();
  if (schedule.enabled && schedule.next_run) {
    const next = new Date(schedule.next_run);
    line.append("Weekly check on. Next run ",
      el("strong", { text: next.toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric" }) }),
      " at 07:00.");
  } else {
    line.textContent = "Weekly check off.";
  }

  const status = $("weeklyStatus");
  const last = schedule.last;
  status.className = "weekly-status";
  if (!last) { status.textContent = "The weekly check hasn't run yet."; return; }
  const when = new Date(last.at).toLocaleString("en-US", { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  if (last.ok) {
    status.textContent = `Last weekly check, ${when}: ${last.message}.`;
  } else {
    status.classList.add("failed");
    status.textContent = `Last weekly check, ${when}, did not finish. ${last.message} ${last.fix || ""}`;
  }
}

const STAGE_TITLE = {
  starting: "Starting",
  syncing: "Fetching the Drive folder",
  reading: "Reading invoices",
  checking: "Checking the rules",
  done: "Done",
  failed: "The check stopped",
};

function renderProgress(run) {
  const box = $("progress");
  const active = run && !["done"].includes(run.stage);
  box.hidden = !active;
  if (!active) return;

  if (run.stage === "failed") {
    box.hidden = true;
    notice("The check stopped.", run.error);
    return;
  }
  const pct = run.total ? Math.round((run.done / run.total) * 100) : (run.stage === "checking" ? 100 : 4);
  $("progressTitle").textContent = run.stage === "reading"
    ? `Reading ${run.done} of ${plural(run.total, "invoice", "invoices")}`
    : STAGE_TITLE[run.stage] || "Working";
  $("progressDetail").textContent = run.message || "";
  $("barFill").style.width = `${pct}%`;
  $("bar").setAttribute("aria-valuenow", String(pct));

  $("fileList").replaceChildren(...run.files.map((f) => el("li", {},
    el("span", { text: f.file }),
    el("span", {
      class: `state ${f.status}`,
      text: f.status === "reading" ? "Reading" : f.status === "error" ? "Couldn't read" : "Read",
    }))));
}

function renderResults(summary) {
  $("results").hidden = false;
  const rows = summary.rows;
  const review = rows.filter((r) => r.flag === "Y");
  const ok = rows.length - review.length;

  $("resultsTitle").textContent = `Results, ${longDate(summary.finished)}`;
  const line = $("resultsLine");
  line.replaceChildren(
    `${plural(summary.files, "file", "files")} checked. `,
    el("strong", { text: `${review.length} need review` }),
    ` before payment, about ${usd.format(summary.usd_review)} of ${usd.format(summary.usd_total)}. `,
    `${ok} had no issues found.`);

  $("dlExcel").href = runUrl(summary.run_id, "excel");
  $("dlCsv").href = runUrl(summary.run_id, "csv");
  $("openFolder").onclick = () => api(`/api/runs/${summary.run_id}/folder`, { method: "POST", body: "{}" })
    .catch((e) => notice("Couldn't open the folder.", e.message));

  const labels = { review: `Needs review (${review.length})`, ok: `No issues found (${ok})`, all: `All (${rows.length})` };
  document.querySelectorAll(".filter").forEach((b) => {
    b.textContent = labels[b.dataset.filter];
    b.setAttribute("aria-pressed", String(b.dataset.filter === view.filter));
  });

  const shown = rows.filter((r) => view.filter === "all" || (view.filter === "review") === (r.flag === "Y"));
  const body = $("rows");
  if (!shown.length) {
    body.replaceChildren(el("tr", {}, el("td", { colspan: "7", class: "empty",
      text: view.filter === "review" ? "Nothing needs review in this run." : "No invoices in this view." })));
  } else {
    body.replaceChildren(...shown.flatMap((r) => rowNodes(summary.run_id, r)));
  }

  const total = shown.reduce((s, r) => s + (r.amount_usd || 0), 0);
  $("totalLabel").textContent = `Total, ${plural(shown.length, "invoice", "invoices")} (USD equivalent)`;
  $("totalAmount").textContent = usd.format(total);
}


function rowNodes(runId, r) {
  const key = r.file;
  const open = view.expanded.has(key);
  const flags = r.flags || [];
  const review = r.flag === "Y";

  const fileLink = el("a", { href: runUrl(runId, "original", `&file=${encodeURIComponent(r.file)}`),
    target: "_blank", rel: "noopener", text: r.file, onclick: (e) => e.stopPropagation() });

  const toggle = () => {
    if (view.expanded.has(key)) view.expanded.delete(key); else view.expanded.add(key);
    renderResults(view.state.latest);
  };
  const row = el("tr", { class: "row", tabindex: "0", "aria-expanded": String(open),
      onclick: toggle, onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } } },
    el("td", { class: "margin" }, el("span", { class: `status ${review ? "review" : "ok"}`, text: review ? "Review" : "No issues" })),
    el("td", { class: "file" }, fileLink),
    el("td", { text: r.vendor }),
    el("td", { class: "nowrap", text: r.invoice_number }),
    el("td", { class: "nowrap", text: longDate(r.due_date) }),
    el("td", { class: "num", text: money(r.amount, r.currency || "USD") }),
    el("td", { class: "why" }, flags.length ? el("ul", {}, flags.map((f) => el("li", { text: f }))) : null));

  if (!open) return [row];
  const notes = r.context || [];
  const detail = el("tr", { class: "detail" },
    el("td", { class: "margin" }),
    el("td", { colspan: "6" }, el("dl", { class: "detail-grid" },
      item("Invoice date as printed", r.invoice_date_raw || "Not shown"),
      item("Payment terms", r.payment_terms || "Not shown"),
      item("How the due date was set", r.due_date_basis === "printed" ? "Printed on the invoice"
        : r.due_date_basis === "unknown" ? "Not found" : `From the payment terms: ${r.due_date_basis}`),
      item("Currency", r.currency === "USD" ? "USD" : `${r.currency}, about ${usd.format(r.amount_usd || 0)}`),
      item("Source", { pdf_text: "Digital PDF", pdf_scan: "Scanned PDF", image: "Scan or photo", email: "Email with attachment" }[r.source_kind] || r.source_kind),
      notes.length ? el("div", { class: "wide" }, el("dt", { text: "Notes" }),
        el("dd", {}, el("ul", { class: "notes" }, notes.map((n) => el("li", { text: n }))))) : null)));
  return [row, detail];
}

function item(label, value, cls) {
  return el("div", { class: cls }, el("dt", { text: label }), el("dd", { text: value }));
}

// ---------- actions ----------

async function refresh(fresh = false) {
  try {
    const state = await api(fresh ? "/api/state?fresh=1" : "/api/state");
    render(state);
    const running = state.run && !["done", "failed"].includes(state.run.stage);
    if (running && !view.polling) poll();
    return state;
  } catch (e) {
    notice("The app isn't responding.", "Close this tab and open Invoice Triage again.");
    return null;
  }
}

async function poll() {
  view.polling = true;
  while (true) {
    await new Promise((r) => setTimeout(r, POLL_MS));
    const state = await refresh();
    if (!state || !state.run || ["done", "failed"].includes(state.run.stage)) break;
  }
  view.polling = false;
  view.filter = "review";
  view.expanded.clear();
  if (view.state && view.state.latest && view.state.run && view.state.run.stage === "done") {
    $("results").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

async function startRun(body) {
  clearNotice();
  try {
    await api("/api/run", { method: "POST", body: JSON.stringify(body) });
    $("results").hidden = true;
    await refresh();
    if (!view.polling) poll();
  } catch (e) {
    notice("The check didn't start.", e.message);
  }
}

$("driveForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const link = $("driveLink").value.trim();
  if (!link) { notice("Paste the Google Drive folder link first.", "In Drive, open the folder, select Share, then Copy link."); return; }
  startRun({ source: "drive", drive_link: link });
});

$("filePick").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  if (!files.length) return;
  clearNotice();
  const id = [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, "0")).join("");
  const box = $("progress");
  box.hidden = false;
  $("fileList").replaceChildren();
  const skipped = [];
  for (const [i, f] of files.entries()) {
    $("progressTitle").textContent = `Adding ${i + 1} of ${plural(files.length, "file", "files")}`;
    $("barFill").style.width = `${Math.round(((i + 1) / files.length) * 100)}%`;
    if (f.size > 25 * 1024 * 1024) { skipped.push(`${f.name} (over 25 MB)`); continue; }
    try {
      await api(`/api/upload/${id}/${encodeURIComponent(f.name)}`, { method: "PUT", body: f });
    } catch (err) {
      skipped.push(`${f.name} (${err.message})`);
    }
  }
  if (skipped.length === files.length) {
    box.hidden = true;
    notice("None of the files could be added.", skipped.join("; "));
    return;
  }
  await startRun({ source: "upload", upload_id: id });
  if (skipped.length) notice(`${plural(skipped.length, "file was", "files were")} not checked; review by hand.`, skipped.join("; "));
});

document.querySelectorAll(".filter").forEach((b) => b.addEventListener("click", () => {
  view.filter = b.dataset.filter;
  if (view.state && view.state.latest) renderResults(view.state.latest);
}));

$("scheduleToggle").addEventListener("change", async (e) => {
  const want = e.target.checked;
  e.target.disabled = true;
  try {
    await api("/api/schedule", { method: "POST", body: JSON.stringify({ enabled: want }) });
    clearNotice();
  } catch (err) {
    notice(want ? "The weekly check couldn't be turned on." : "The weekly check couldn't be turned off.", err.message);
  } finally {
    e.target.disabled = false;
    refresh(true);
  }
});

refresh(true);

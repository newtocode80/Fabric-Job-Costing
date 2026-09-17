"use strict";

const form = document.getElementById("ask");
const input = document.getElementById("question");
const submit = document.getElementById("submit");
const statusEl = document.getElementById("status");
const answerEl = document.getElementById("answer");
const noticeEl = document.getElementById("notice");
const detail = document.getElementById("detail");
const summary = document.getElementById("summary");
const panel = document.getElementById("panel");

/** Everything from the API is data, never markup. */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

/** Right-align values that are numbers, including the Decimal strings money uses. */
function isNumeric(value) {
  return typeof value === "number" ||
    (typeof value === "string" && value !== "" && /^-?\d+(\.\d+)?$/.test(value));
}

function renderAnswer(text) {
  answerEl.replaceChildren();
  const paragraphs = String(text || "").split(/\n\s*\n/).filter((p) => p.trim());
  for (const p of paragraphs) answerEl.append(el("p", null, p.trim()));
}

function renderTable(columns, rows) {
  const table = el("table");
  const head = el("tr");
  for (const c of columns) head.append(el("th", null, c));
  // Node.append() returns undefined, so each node is built then attached.
  const thead = el("thead");
  thead.append(head);
  table.append(thead);

  const body = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    row.forEach((value) => {
      const cell = el("td", isNumeric(value) ? "num" : null,
        value === null ? "—" : String(value));
      tr.append(cell);
    });
    body.append(tr);
  }
  table.append(body);

  const scroll = el("div", "scroll");
  scroll.append(table);
  return scroll;
}

function renderPanel(data) {
  panel.replaceChildren();
  const calls = data.tool_calls || [];

  calls.forEach((call, i) => {
    const block = el("div", "attempt");
    if (calls.length > 1) {
      block.append(el("h3", null, `Query ${i + 1} of ${calls.length}`));
    }
    block.append(el("p", "label", call.rejected ? "Rejected — did not run" : "SQL"));
    block.append(el("pre", null, (call.executed_sql || call.sql).trim()));

    if (call.clamped_from) {
      block.append(el("div", "flag",
        `LIMIT ${call.clamped_from} was clamped to the ${data.row_limit || 500}-row cap. ` +
        `The query above wraps what the model wrote, which is shown unchanged inside it.`));
    }
    if (call.error) {
      block.append(el("div", call.rejected ? "flag rejected" : "flag", call.error));
    }
    if (call.truncated) {
      block.append(el("div", "flag",
        `The model was shown part of this result, and told the true row count.`));
    }
    panel.append(block);
  });

  if (data.columns && data.columns.length && data.rows) {
    panel.append(el("p", "label", "Rows returned"));
    panel.append(renderTable(data.columns, data.rows));
    panel.append(el("p", "rowcount",
      `${data.rows.length} row${data.rows.length === 1 ? "" : "s"}`));
  }

  const queries = calls.length === 1 ? "1 query" : `${calls.length} queries`;
  const rows = (data.rows || []).length;
  summary.textContent = `SQL and returned rows — ${queries}, ${rows} row${rows === 1 ? "" : "s"}`;
}

async function ask(question) {
  submit.disabled = true;
  input.disabled = true;
  statusEl.hidden = false;
  statusEl.textContent = "Working — writing SQL and running it…";
  answerEl.replaceChildren();
  noticeEl.replaceChildren();
  detail.hidden = true;

  try {
    const response = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!response.ok) {
      throw new Error(`The server returned ${response.status} ${response.statusText}.`);
    }
    const data = await response.json();

    renderAnswer(data.answer);
    if (data.error) {
      noticeEl.append(el("div", "notice", data.error));
    }
    if ((data.tool_calls || []).length) {
      renderPanel(data);
      detail.hidden = false;
    }
  } catch (err) {
    noticeEl.append(el("div", "notice", String(err.message || err)));
  } finally {
    statusEl.hidden = true;
    submit.disabled = false;
    input.disabled = false;
    input.focus();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (question) ask(question);
});

for (const button of document.querySelectorAll(".example")) {
  button.addEventListener("click", () => {
    input.value = button.textContent;
    ask(input.value);
  });
}

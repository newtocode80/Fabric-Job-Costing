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

/* ---------------------------------------------------------------- markdown

   The model writes prose with light markdown. Rendering it as plain text shows
   literal asterisks; rendering it with innerHTML would make model output into
   markup. So this builds real DOM nodes and never assigns HTML, which keeps the
   guarantee that nothing from the API is ever interpreted as markup.

   Deliberately small: the subset the prose actually uses -- paragraphs, bold,
   italic, inline code, bullet and numbered lists, and fenced code. The system
   prompt already tells the model not to emit tables or SQL in the answer.
*/

const INLINE = /(\*\*|__)(.+?)\1|(\*|_)(.+?)\3|`([^`]+)`/g;

function inlineNodes(text) {
  const nodes = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) nodes.push(document.createTextNode(text.slice(last, m.index)));
    if (m[2] !== undefined) nodes.push(el("strong", null, m[2]));
    else if (m[4] !== undefined) nodes.push(el("em", null, m[4]));
    else nodes.push(el("code", null, m[5]));
    last = m.index + m[0].length;
  }
  if (last < text.length) nodes.push(document.createTextNode(text.slice(last)));
  return nodes;
}

function listItem(line) {
  const li = el("li");
  li.append(...inlineNodes(line));
  return li;
}

function renderMarkdown(text, target) {
  target.replaceChildren();
  const lines = String(text || "").split("\n");
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    if (line.trimStart().startsWith("```")) {              // fenced code
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trimStart().startsWith("```")) body.push(lines[i++]);
      i++;
      target.append(el("pre", null, body.join("\n")));
      continue;
    }

    const bullet = /^\s*[-*+]\s+(.*)$/;
    const number = /^\s*\d+[.)]\s+(.*)$/;
    const kind = bullet.test(line) ? bullet : number.test(line) ? number : null;
    if (kind) {                                            // a run of list items
      const list = el(kind === bullet ? "ul" : "ol");
      while (i < lines.length && kind.test(lines[i])) {
        list.append(listItem(lines[i].match(kind)[1]));
        i++;
      }
      target.append(list);
      continue;
    }

    const para = [];                                       // paragraph until blank
    while (i < lines.length && lines[i].trim() &&
           !bullet.test(lines[i]) && !number.test(lines[i]) &&
           !lines[i].trimStart().startsWith("```")) {
      para.push(lines[i].trim());
      i++;
    }
    const p = el("p");
    p.append(...inlineNodes(para.join(" ")));
    target.append(p);
  }
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

    renderMarkdown(data.answer, answerEl);
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

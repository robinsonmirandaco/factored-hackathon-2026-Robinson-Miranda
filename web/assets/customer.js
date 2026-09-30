// Customer web (TRZ-34): login, home, movements, the clarification chat and Mis aclaraciones.
// Every figure on screen is read from the API; the page holds no customer data of its own.
// Text reaches the page only through textContent, never as HTML.

import { createClient } from "./api.js";
import { day, dayTime, label, money, translator } from "./i18n.js";
import {
  buttonMessage, chipParts, clarificationLines, codeStep, errorText, pendingButtons,
  pendingQuestion, replyLines,
} from "./view.js";

const api = createClient("customer");
const DOCUMENT_TYPES = ["CURP", "INE", "CC", "CE", "DNI", "Pasaporte"];
const CASE_KEY = "trazo.customer.case";
const LANG_KEY = "trazo.lang";

const state = {
  lang: api.store.read(LANG_KEY) === "pt" ? "pt" : "es",
  me: null,
  caseId: api.store.read(CASE_KEY),
  nextBefore: null,
  busy: false,
};
let t = translator(state.lang);

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function setCase(id) {
  state.caseId = id;
  api.store.write(CASE_KEY, id);
}

// ---- language and chrome ------------------------------------------------------------------

function applyTexts() {
  document.documentElement.lang = state.lang === "pt" ? "pt-BR" : "es";
  for (const node of document.querySelectorAll("[data-t]")) node.textContent = t(node.dataset.t);
  $("message").placeholder = t("placeholder");
  for (const b of document.querySelectorAll("[data-lang]")) {
    b.setAttribute("aria-pressed", String(b.dataset.lang === state.lang));
  }
  if (state.me) $("clock").textContent = `${t("simulatedDate")}: ${day(state.lang, state.me.now)}`;
}

function setLang(lang) {
  state.lang = lang;
  api.store.write(LANG_KEY, lang);
  t = translator(lang);
  applyTexts();
  route();
}

function showChrome(loggedIn) {
  $("tabs").hidden = !loggedIn;
  $("logout").hidden = !loggedIn;
  $("clock").hidden = !loggedIn || !state.me;
}

// ---- login (also used by the expired session dialog) --------------------------------------

function loginForm(form, onDone) {
  form.replaceChildren();
  const type = el("select", { id: `${form.id}-type`, required: true },
    DOCUMENT_TYPES.map((d) => el("option", { value: d, text: d })));
  const number = el("input", { id: `${form.id}-number`, autocomplete: "off", maxlength: 32, required: true });
  const code = el("input", {
    id: `${form.id}-code`, inputmode: "numeric", autocomplete: "one-time-code",
    maxlength: 6, pattern: "\\d{6}",
  });
  const codeRow = el("div", { hidden: true },
    el("label", { for: code.id, text: t("code") }), code);
  const info = el("p", { class: "muted small", "aria-live": "polite" });
  const error = el("p", { class: "error", role: "alert" });
  const askButton = el("button", { type: "button", class: "btn", text: t("requestCode") });
  const enterButton = el("button", { type: "submit", class: "btn primary", text: t("verify"), hidden: true });
  const doc = () => ({ document_type: type.value, document_number: number.value.trim() });
  let requestedFor = null;
  // A code belongs to the document it was asked for: changing the document hides the field.
  const sync = () => {
    const { showCode } = codeStep(requestedFor, { type: type.value, number: number.value.trim() });
    codeRow.hidden = !showCode;
    enterButton.hidden = !showCode;
    code.disabled = !showCode;
    if (!showCode) code.value = "";
    info.textContent = showCode ? info.textContent : t("codeFirst");
  };
  type.addEventListener("change", () => { requestedFor = null; sync(); });
  number.addEventListener("input", sync);

  askButton.addEventListener("click", async () => {
    error.textContent = "";
    if (!doc().document_number) return number.focus();
    askButton.disabled = true;
    try {
      const r = await api.call("/auth/otp/request", { method: "POST", body: doc() });
      requestedFor = { type: type.value, number: number.value.trim() };
      sync();
      info.textContent = t("codeSent", { min: Math.round(r.expires_in_seconds / 60) });
      code.focus();
    } catch (e) {
      error.textContent = errorText(t, e);
    } finally {
      askButton.disabled = false;
    }
  });

  form.onsubmit = async (event) => {
    event.preventDefault();
    error.textContent = "";
    if (codeRow.hidden) return askButton.click();
    enterButton.disabled = true;
    try {
      const r = await api.call("/auth/otp/verify", {
        method: "POST", body: { ...doc(), code: code.value.trim() },
      });
      api.setToken(r.access_token);
      await onDone();
    } catch (e) {
      error.textContent = errorText(t, e);
    } finally {
      enterButton.disabled = false;
    }
  };

  form.append(
    el("p", { class: "muted", text: t("loginIntro") }),
    el("div", {}, el("label", { for: type.id, text: t("documentType") }), type),
    el("div", {}, el("label", { for: number.id, text: t("documentNumber") }), number),
    codeRow,
    el("div", { class: "row" }, askButton, enterButton),
    info,
    error,
    el("p", { class: "note", text: t("identityNote") }),
  );
  sync();
}

async function loadMe() {
  state.me = await api.call("/me");
  $("audit").hidden = !state.me.demo;
  applyTexts();
  showChrome(true);
}

// ---- views --------------------------------------------------------------------------------

const VIEWS = {
  "#/inicio": "home",
  "#/movimientos": "movements",
  "#/aclarar": "chat",
  "#/aclaraciones": "clarifications",
};

async function route() {
  if (!api.hasSession()) {
    showChrome(false);
    return show("login");
  }
  if (!state.me) {
    try {
      await loadMe();
    } catch {
      return;
    }
  }
  const view = VIEWS[location.hash] || "home";
  show(view);
  if (view === "home") renderHome();
  if (view === "movements") renderMovements(true);
  if (view === "clarifications") renderClarifications();
}

function show(view) {
  for (const name of ["login", "home", "movements", "chat", "clarifications"]) {
    $(`view-${name}`).hidden = name !== view;
  }
  for (const a of document.querySelectorAll("#tabs a")) {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  if (view === "login") loginForm($("login-form"), async () => {
    await loadMe();
    location.hash = "#/inicio";
    route();
  });
}

function failure(target, e) {
  target.replaceChildren(el("div", { class: "card" },
    el("p", { class: "error", text: errorText(t, e) }),
    e?.traceId ? el("p", { class: "muted small mono", text: `trace_id ${e.traceId}` }) : null));
}

function amountCell(item) {
  const main = el("span", { text: money(state.lang, item.amount, item.currency) });
  const approx = item.converted_amount != null
    ? el("span", {
      class: "approx",
      text: `≈ ${money(state.lang, item.converted_amount, item.converted_currency)} (${t("approx")})`,
    })
    : null;
  return [main, approx];
}

async function renderHome() {
  const target = $("view-home");
  try {
    const [products, clarifications] = await Promise.all([
      api.call("/me/products"), api.call("/me/clarifications"),
    ]);
    const name = state.me.first_name;
    const cards = products.length
      ? el("div", { class: "grid" }, products.map((p) => el("div", { class: "card" },
        el("div", { class: "muted small", text: label(t, "product", p.product_type) }),
        el("div", { class: "mono", text: p.last4 ? `•••• ${p.last4}` : "" }),
        p.current_balance != null
          ? el("div", {}, `${t("balance")}: `, el("strong", { text: money(state.lang, p.current_balance, p.currency) }))
          : null,
        p.credit_limit != null
          ? el("div", { class: "small muted", text: `${t("limit")}: ${money(state.lang, p.credit_limit, p.currency)}` })
          : null,
        el("div", { class: "small muted", text: label(t, "pstatus", p.status) }))))
      : el("p", { class: "muted", text: t("noProducts") });
    target.replaceChildren(
      el("div", { class: "card" },
        el("h2", { text: name ? t("hello", { name }) : t("helloAnon") }),
        el("p", { class: "muted", text: t("homeActions") }),
        el("div", { class: "row" },
          el("a", { class: "btn primary", href: "#/aclarar", text: t("navChat") }),
          el("a", { class: "btn", href: "#/movimientos", text: t("navMovements") }),
          el("a", { class: "btn", href: "#/aclaraciones", text: `${t("navClarifications")} (${clarifications.length})` }))),
      el("div", { class: "card" }, el("h2", { text: t("products") }), cards),
    );
  } catch (e) {
    failure(target, e);
  }
}

async function renderMovements(reset) {
  const target = $("view-movements");
  if (reset) {
    state.nextBefore = null;
    target.replaceChildren(el("div", { class: "card" },
      el("h2", { text: t("movements") }),
      el("table", { class: "table" },
        el("thead", {}, el("tr", {},
          el("th", { text: t("date") }), el("th", { text: t("detail") }),
          el("th", { class: "amount", text: t("amount") }),
          el("th", { class: "hide-sm", text: t("status") }), el("th", {}))),
        el("tbody", { id: "movements-body" })),
      el("div", { class: "row", id: "movements-more" })));
  }
  const body = $("movements-body");
  const more = $("movements-more");
  try {
    const query = new URLSearchParams({ limit: "30" });
    if (state.nextBefore) query.set("before", state.nextBefore);
    const page = await api.call(`/me/transactions?${query}`);
    if (reset && !page.items.length) {
      body.append(el("tr", {}, el("td", { colspan: 5, class: "muted", text: t("noMovements") })));
    }
    for (const m of page.items) {
      const pending = m.status === "Pending";
      const action = m.disputable
        ? el("button", {
          type: "button", class: `btn small${pending ? "" : " primary"}`,
          text: pending ? t("whatIsThis") : t("notRecognized"),
          onclick: () => openByButton(m.transaction_id, buttonMessage(t, state.lang, m)),
        })
        : null;
      body.append(el("tr", {},
        el("td", { text: dayTime(state.lang, m.at) }),
        el("td", {},
          el("div", { text: m.merchant || label(t, "type", m.transaction_type) }),
          el("div", { class: "muted small", text: [label(t, "type", m.transaction_type), m.city, label(t, "channel", m.channel)].filter(Boolean).join(" · ") })),
        el("td", { class: "amount" }, amountCell(m)),
        el("td", { class: "hide-sm" }, el("span", {
          class: `pill${pending ? " warn" : ""}`, text: label(t, "tx", m.status),
        })),
        el("td", {}, action)));
    }
    state.nextBefore = page.next_before;
    more.replaceChildren(page.next_before
      ? el("button", { type: "button", class: "btn", text: t("loadMore"), onclick: () => renderMovements(false) })
      : "");
  } catch (e) {
    failure(target, e);
  }
}

async function renderClarifications() {
  const target = $("view-clarifications");
  try {
    const items = await api.call("/me/clarifications");
    const list = items.length
      ? items.map((c) => {
        const { title, ref } = clarificationLines(t, state.lang, c);
        const due = c.due_date
          ? el("span", {
            class: `pill${c.overdue ? " bad" : ""}`,
            text: c.overdue ? t("overdueSince", { date: day(state.lang, c.due_date) }) : t("dueBy", { date: day(state.lang, c.due_date) }),
          })
          : el("span", { class: "muted small", text: t("noDeadline") });
        return el("div", { class: "card" },
          el("div", { class: "row" },
            el("strong", { text: title }), el("span", { class: "spacer" }),
            el("span", { class: "pill warn", text: label(t, "status", c.status) })),
          el("div", { class: "row small" },
            c.opened_on ? el("span", { class: "muted", text: t("openedOn", { date: day(state.lang, c.opened_on) }) }) : null,
            due),
          el("div", { class: "muted small mono", text: ref }));
      })
      : [el("div", { class: "card" }, el("p", { class: "muted", text: t("noClarifications") }))];
    target.replaceChildren(
      el("h2", { text: t("clarificationsTitle") }),
      ...list,
      el("p", { class: "note", text: t("deadlineNote") }));
  } catch (e) {
    failure(target, e);
  }
}

// ---- chat ---------------------------------------------------------------------------------

function bubble(who, ...children) {
  const node = el("div", { class: `msg ${who}` }, ...children);
  $("log").append(node);
  node.scrollIntoView({ block: "end", behavior: "smooth" });
  return node;
}

function greet() {
  $("log").replaceChildren();
  bubble("bot", t("chatIntro"));
}

function chargeCard(c) {
  const rows = [
    ["field_merchant", c.merchant || label(t, "type", c.transaction_type)],
    ["field_amount", el("span", {}, amountCell(c))],
    ["field_at", dayTime(state.lang, c.at)],
    ["field_city", c.city],
    ["field_channel", label(t, "channel", c.channel)],
    ["field_product", `${label(t, "product", c.product_type)}${c.last4 ? ` •••• ${c.last4}` : ""}`],
    ["field_status", label(t, "tx", c.status)],
  ].filter(([, v]) => v);
  return el("div", { class: "charge" },
    el("dl", {}, rows.flatMap(([k, v]) => [el("dt", { text: t(k) }), el("dd", {}, v)])));
}

// What the system read, each value with the literal fragment of the message it came from.
function chipRow(clues) {
  if (!clues.length) return null;
  return el("div", { class: "chips" },
    el("span", { class: "muted small", text: `${t("understood")}:` }),
    clues.map((c) => {
      const chip = chipParts(t, state.lang, c);
      return el("span", { class: "chip", title: c.evidence },
        el("strong", { text: `${chip.label}: ` }), chip.value,
        chip.evidence ? el("span", { class: "evidence", text: ` «${chip.evidence}»` }) : null);
    }));
}

function renderTurn(r) {
  $("last-trace").textContent = r.trace_id;
  const lines = replyLines(r.reply).map((line) => (line.simulated
    ? el("span", { class: "sim-label" }, el("span", { class: "badge sim", text: "simulado" }), ` ${line.text}`)
    : el("span", { class: "line", text: line.text })));
  const parts = [chipRow(r.clues), ...lines];
  if (r.charge) parts.push(chargeCard(r.charge));
  const buttons = [];
  // The API sends the primary choice first ("Sigo sin reconocerlo", design 10.2 rule 4).
  r.choices.forEach((c, i) => buttons.push(el("button", {
    type: "button", class: `btn${i === 0 ? " primary" : ""}`, text: c.label,
    onclick: () => send({ message: c.label, case_id: r.case_id, recognition: c.id }, c.label),
  })));
  for (const o of r.options) {
    const text = `${o.merchant || ""} · ${money(state.lang, o.amount, o.currency)} · ${day(state.lang, o.date)}`;
    buttons.push(el("button", {
      type: "button", class: "btn", text,
      onclick: () => send({ message: text, case_id: r.case_id, option: o.transaction_id }, text),
    }));
  }
  if (r.options.length) {
    buttons.push(el("button", {
      type: "button", class: "btn", text: t("noneOfThese"),
      onclick: () => send({ message: t("noneOfThese"), case_id: r.case_id, option: "none" }, t("noneOfThese")),
    }));
  }
  for (const c of r.claims) {
    const text = `${c.claim_id} · ${day(state.lang, c.opened_on)}`;
    buttons.push(el("button", {
      type: "button", class: "btn", text,
      onclick: () => send({ message: text, case_id: r.case_id, option: c.claim_id }, text),
    }));
  }
  if (buttons.length) parts.push(el("div", { class: "choices" }, buttons));
  if (r.pending_action) {
    const p = r.pending_action;
    const answer = { confirm: "confirm_action_id", decline: "decline_action_id" };
    parts.push(el("p", { class: "question", text: pendingQuestion(t, state.lang, p) }));
    parts.push(el("div", { class: "choices" }, pendingButtons(t, p).map((b) => el("button", {
      type: "button", class: `btn${b.primary ? " primary" : ""}`, text: b.label,
      onclick: () => send({ message: b.label, case_id: r.case_id, [answer[b.kind]]: p.action_id }, b.label),
    }))));
  }
  if (r.dispute_folio) {
    parts.push(el("div", { class: "folio" }, el("span", { class: "pill" },
      `${t("folio")} `, el("span", { class: "mono", text: r.dispute_folio }), ` · ${t("verified")}`)));
  }
  parts.push(el("span", { class: "meta", text: `${t("caseLabel")} ${r.case_id} · trace_id ${r.trace_id}` }));
  bubble("bot", ...parts);
}

async function send(body, shown) {
  if (state.busy) return;
  state.busy = true;
  bubble("me", shown);
  for (const b of $("log").querySelectorAll(".choices button")) b.disabled = true;
  try {
    const r = await api.call("/chat", { method: "POST", body });
    setCase(r.case_id);
    renderTurn(r);
  } catch (e) {
    if (e.code === "case_not_found") setCase(null);
    bubble("bot", el("span", { class: "error", text: errorText(t, e) }),
      e.traceId ? el("span", { class: "meta", text: `trace_id ${e.traceId}` }) : null);
  } finally {
    state.busy = false;
  }
}

function openByButton(transactionId, text) {
  location.hash = "#/aclarar";
  setCase(null);
  greet();
  send({ message: text, transaction_id: transactionId }, text);
}

// ---- wiring -------------------------------------------------------------------------------

$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("message").value.trim();
  if (!text) return;
  $("message").value = "";
  const body = { message: text };
  if (state.caseId) body.case_id = state.caseId;
  send(body, text);
});

$("new-case").addEventListener("click", () => {
  setCase(null);
  greet();
});

$("cross-access").addEventListener("click", () => {
  // Names someone else in the body: the API ignores it for data and stops the case for
  // security (TRZ-09 CA6). Design 10.3, step 5.
  const text = t("crossAccessMessage");
  setCase(null);
  send({ message: text, customer_id: "CUSTOMER-OF-SOMEONE-ELSE" }, `${t("crossAccess")}`);
});

for (const b of document.querySelectorAll("[data-lang]")) {
  b.addEventListener("click", () => setLang(b.dataset.lang));
}

$("logout").addEventListener("click", async () => {
  try {
    await api.call("/auth/logout", { method: "POST" });
  } catch {
    // The session may already be gone; the local token is dropped either way.
  }
  api.clear();
  setCase(null);
  state.me = null;
  greet();
  location.hash = "";
  route();
});

window.addEventListener("trazo:session-lost", () => {
  if (!$("expired").hidden) return;
  api.clear();
  $("expired").hidden = false;
  loginForm($("expired-form"), async () => {
    $("expired").hidden = true;
    await loadMe();
    route();
  });
});

window.addEventListener("hashchange", route);
applyTexts();
greet();
route();

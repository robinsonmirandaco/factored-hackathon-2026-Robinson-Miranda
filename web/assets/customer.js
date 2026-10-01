// Customer web (TRZ-34): login, home, movements, the clarification chat and Mis aclaraciones.
// Every figure on screen is read from the API; the page holds no customer data of its own.
// Text reaches the page only through textContent, never as HTML.

import { createClient } from "./api.js";
import { day, dayTime, label, money, translator } from "./i18n.js";
import {
  buttonMessage, canSend, clarificationLines, closedNote, codeStep, createConversation, deadlineKind,
  errorText,
  infoRequestViews, movementDetail, openQuestions, reviewLine, statusTone, turnModel,
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
  lastTurn: null,
};
let t = translator(state.lang);

const $ = (id) => document.getElementById(id);
const talk = createConversation();

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
  redrawLastTurn();
  route();
}

function showChrome(loggedIn) {
  $("appbar").hidden = !loggedIn;
  $("clock").hidden = !loggedIn || !state.me;
  $("avatar").textContent = (state.me?.first_name || "").slice(0, 1).toUpperCase();
}

// ---- login (also used by the expired session dialog) --------------------------------------

function loginForm(form, onDone) {
  form.replaceChildren();
  const type = el("select", { id: `${form.id}-type`, required: true, "aria-label": t("documentType") },
    DOCUMENT_TYPES.map((d) => el("option", { value: d, text: d })));
  const number = el("input", {
    id: `${form.id}-number`, autocomplete: "off", maxlength: 32, required: true, placeholder: t("documentNumber"),
  });
  const code = el("input", {
    id: `${form.id}-code`, class: "code-input", inputmode: "numeric", autocomplete: "one-time-code",
    maxlength: 6, pattern: "\\d{6}", placeholder: "000000",
  });
  const codeRow = el("div", { hidden: true },
    el("label", { for: code.id, text: t("code") }), code);
  const info = el("p", { class: "muted small", "aria-live": "polite" });
  const error = el("p", { class: "error", role: "alert" });
  const askButton = el("button", { type: "button", class: "btn primary block", text: t("requestCode") });
  const enterButton = el("button", { type: "submit", class: "btn primary block", text: t("verify"), hidden: true });
  const doc = () => ({ document_type: type.value, document_number: number.value.trim() });
  let requestedFor = null;
  // A code belongs to the document it was asked for: changing the document hides the field.
  const sync = () => {
    const { showCode } = codeStep(requestedFor, { type: type.value, number: number.value.trim() });
    codeRow.hidden = !showCode;
    enterButton.hidden = !showCode;
    askButton.className = showCode ? "btn block" : "btn primary block";
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
    code.removeAttribute("aria-invalid");
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
      code.setAttribute("aria-invalid", "true");
    } finally {
      enterButton.disabled = false;
    }
  };

  form.append(
    el("p", { class: "muted small", text: t("loginIntro") }),
    el("div", {},
      el("label", { for: number.id, text: `${t("documentType")} · ${t("documentNumber")}` }),
      el("div", { class: "doc-field" }, type, number)),
    codeRow,
    enterButton,
    askButton,
    info,
    error,
    el("p", { class: "notice", text: t("identityNote") }),
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

// The dot on "Mis aclaraciones": questions of an analyst waiting for an answer (TRZ-28).
function markQuestions(items) {
  const n = openQuestions(items);
  const dot = $("questions-dot");
  dot.hidden = n === 0;
  dot.textContent = String(n);
  dot.title = t("pendingQuestions", { n });
  return n;
}

function amountCell(item) {
  const main = el("span", { class: "amount", text: money(state.lang, item.amount, item.currency) });
  const approx = item.converted_amount != null
    ? el("span", {
      class: "approx",
      text: `≈ ${money(state.lang, item.converted_amount, item.converted_currency)} (${t("approx")})`,
    })
    : null;
  return [main, approx];
}

const initialOf = (text) => String(text || "?").trim().slice(0, 1).toUpperCase();

function statusPill(c) {
  return el("span", { class: `pill ${statusTone(c.status)}`, text: label(t, "status", c.status) });
}

async function renderHome() {
  const target = $("view-home");
  try {
    const [products, clarifications] = await Promise.all([
      api.call("/me/products"), api.call("/me/clarifications"),
    ]);
    const questions = markQuestions(clarifications);
    const name = state.me.first_name;
    const productRows = products.length
      ? products.map((p) => el("div", { class: "product" },
        el("div", { class: "row" },
          el("strong", { text: label(t, "product", p.product_type) }),
          p.last4 ? el("span", { class: "mono small muted", text: `•••• ${p.last4}` }) : null,
          el("span", { class: "spacer" }),
          el("span", {
            class: `pill ${p.status === "Active" ? "ok" : p.status === "Blocked" ? "bad" : ""}`,
            text: label(t, "pstatus", p.status),
          })),
        p.current_balance != null
          ? [el("span", { class: "small muted", text: t("balance") }),
            el("span", { class: "figure", text: money(state.lang, p.current_balance, p.currency) })]
          : null,
        p.credit_limit != null
          ? el("span", { class: "small muted", text: `${t("limit")}: ${money(state.lang, p.credit_limit, p.currency)}` })
          : null))
      : [el("p", { class: "muted", text: t("noProducts") })];
    const recent = clarifications.slice(0, 3).map((c) => {
      const { title, ref } = clarificationLines(t, state.lang, c);
      return el("div", { class: "claim-mini" },
        el("div", {},
          el("span", { class: "small", text: title }),
          ref ? el("span", { class: "tiny muted mono", text: ref }) : null),
        statusPill(c));
    });
    target.replaceChildren(
      el("div", { class: "page-head" },
        el("span", { class: "eyebrow", text: day(state.lang, state.me.now) }),
        el("h1", { text: name ? t("hello", { name }) : t("helloAnon") })),
      el("div", { class: "grid-2" },
        el("section", { class: "card tight" }, el("h2", { text: t("products") }), el("div", {}, productRows)),
        el("div", { class: "stack-gap" },
          el("section", { class: "card soft" },
            el("h2", { class: "soft-title", text: t("reviewChargeTitle") }),
            el("p", { class: "soft-body", text: t("reviewChargeBody") }),
            el("div", {}, el("a", { class: "btn primary medium", href: "#/aclarar", text: t("reviewChargeCta") }))),
          questions
            ? el("p", { class: "notice bad" }, el("a", { href: "#/aclaraciones", text: t("pendingQuestions", { n: questions }) }))
            : null,
          el("section", { class: "card tight" },
            el("div", { class: "card-head" },
              el("h2", { text: t("navClarifications") }),
              el("a", { class: "link", href: "#/aclaraciones", text: t("see") })),
            recent.length ? el("div", {}, recent) : el("p", { class: "muted small", text: t("noClarifications") })))),
    );
  } catch (e) {
    failure(target, e);
  }
}

async function renderMovements(reset) {
  const target = $("view-movements");
  if (reset) {
    state.nextBefore = null;
    target.replaceChildren(
      el("div", { class: "page-head" }, el("h1", { text: t("movements") })),
      el("section", { class: "card flush" }, el("ul", { class: "list", id: "movements-body" })),
      el("div", { class: "row more", id: "movements-more" }));
  }
  const body = $("movements-body");
  const more = $("movements-more");
  try {
    const query = new URLSearchParams({ limit: "30" });
    if (state.nextBefore) query.set("before", state.nextBefore);
    const page = await api.call(`/me/transactions?${query}`);
    if (reset && !page.items.length) {
      body.append(el("li", { class: "empty", text: t("noMovements") }));
    }
    for (const m of page.items) {
      const pending = m.status === "Pending";
      const what = m.merchant || label(t, "type", m.transaction_type);
      const action = m.disputable
        ? el("button", {
          type: "button", class: "btn small outline",
          text: pending ? t("whatIsThis") : t("notRecognized"),
          onclick: () => openByButton(m.transaction_id, buttonMessage(t, state.lang, m)),
        })
        : null;
      body.append(el("li", { class: "list-row" },
        el("span", { class: "initial", text: initialOf(what) }),
        el("span", { class: "main" },
          el("span", { class: "title", text: what }),
          el("span", { class: "sub", text: [dayTime(state.lang, m.at), movementDetail(t, m)].filter(Boolean).join(" · ") })),
        el("span", { class: `pill${pending ? " warn" : ""}`, text: label(t, "tx", m.status) }),
        el("span", { class: "end" }, amountCell(m)),
        action));
    }
    state.nextBefore = page.next_before;
    more.replaceChildren(page.next_before
      ? el("button", { type: "button", class: "btn medium", text: t("loadMore"), onclick: () => renderMovements(false) })
      : "");
  } catch (e) {
    failure(target, e);
  }
}

async function renderClarifications() {
  const target = $("view-clarifications");
  try {
    const items = await api.call("/me/clarifications");
    markQuestions(items);
    const list = items.length
      ? items.map((c) => {
        const { title, ref } = clarificationLines(t, state.lang, c);
        const review = reviewLine(t, c);
        const kind = deadlineKind(c);
        const due = kind === "answer" || kind === "closed"
          ? null
          : kind === "review"
          ? el("span", { class: "sim-label" }, el("span", { class: "pill warn", text: review.text }),
            el("span", { class: "sim", text: "simulado" }), review.label)
          : kind === "due"
          ? el("span", {
            class: `pill${c.overdue ? " bad" : ""}`,
            text: c.overdue ? t("overdueSince", { date: day(state.lang, c.due_date) }) : t("dueBy", { date: day(state.lang, c.due_date) }),
          })
          : el("span", { class: "small muted", text: t("noDeadline") });
        return el("section", { class: "card" },
          el("div", { class: "claim-head" },
            el("div", {},
              ref ? el("span", { class: "tiny muted mono", text: ref }) : null,
              el("span", { class: "title", text: title }),
              c.opened_on ? el("span", { class: "small muted", text: t("openedOn", { date: day(state.lang, c.opened_on) }) }) : null),
            statusPill(c)),
          due,
          infoBlock(c),
          closedNote(t, c)
            ? el("div", { class: "info-request" },
              el("p", { class: "small", text: closedNote(t, c) }),
              el("div", {}, el("a", { class: "btn medium", href: "#/aclarar", text: t("navChat") })))
            : null);
      })
      : [el("section", { class: "card" }, el("p", { class: "muted", text: t("noClarifications") }))];
    target.replaceChildren(
      el("div", { class: "page-head" },
        el("span", { class: "eyebrow", text: t("followUp") }),
        el("h1", { text: t("clarificationsTitle") })),
      el("div", { class: "claims" }, list),
      el("p", { class: "page-note", text: t("deadlineNote") }));
  } catch (e) {
    failure(target, e);
  }
}

// The analyst's question and the form to answer it, on the clarification it belongs to.
function infoBlock(item) {
  const views = infoRequestViews(t, state.lang, item);
  if (!views.length) return null;
  return el("div", { class: "info-request" }, views.map((info) => infoEntry(item, info)));
}

function infoEntry(item, info) {
  const head = [
    el("span", { class: "small muted", text: t("infoTitle") }),
    el("p", { class: "question", text: info.question }),
  ];
  if (!info.canAnswer) {
    return el("div", { class: "info-entry" }, head,
      info.answer
        ? [el("span", { class: "small muted", text: t("yourAnswer") }), el("p", { class: "answer", text: info.answer })]
        : null,
      info.answered ? el("p", { class: "small muted", text: info.answered }) : null);
  }
  const answer = el("textarea", { maxlength: "2000", placeholder: t("answerPlaceholder"), "aria-label": t("answerPlaceholder") });
  const button = el("button", { type: "submit", class: "btn primary medium", text: t("sendAnswer"), disabled: true });
  const error = el("p", { class: "error", hidden: true });
  answer.addEventListener("input", () => { button.disabled = !canSend(answer.value); });
  const form = el("form", { class: "stack" }, answer, el("div", { class: "row" }, button), error);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!canSend(answer.value)) return;
    button.disabled = true;
    try {
      await api.call(`/me/clarifications/${encodeURIComponent(item.case_id)}/reply`, {
        method: "POST", body: { text: answer.value.trim() },
      });
      renderClarifications();
    } catch (e) {
      error.textContent = errorText(t, e);
      error.hidden = false;
      button.disabled = false;
    }
  });
  return el("div", { class: "info-entry" }, head,
    el("span", { class: `pill${info.overdue ? " bad" : " warn"}`, text: info.due }), form);
}

// ---- chat ---------------------------------------------------------------------------------

// A message of the customer is one blue bubble; a message of the assistant is a column next to
// its mark, which returns so a turn can be redrawn in place.
function bubble(who, ...children) {
  if (who === "me") {
    const node = el("div", { class: "msg me" }, el("div", { class: "bubble" }, ...children));
    $("log").append(node);
    node.scrollIntoView({ block: "end", behavior: "smooth" });
    return node;
  }
  const turn = el("div", { class: "turn" }, ...children);
  const node = el("div", { class: "msg bot" }, el("span", { class: "bot-mark", "aria-hidden": "true", text: "T" }), turn);
  $("log").append(node);
  node.scrollIntoView({ block: "end", behavior: "smooth" });
  return turn;
}

function greet() {
  $("log").replaceChildren();
  bubble("bot", el("div", { class: "bubble", text: t("chatIntro") }));
}

function chargeCard(rows) {
  const by = Object.fromEntries(rows.map((row) => [row.key, row]));
  const rest = rows.filter((row) => row.key !== "merchant" && row.key !== "amount");
  return el("div", { class: "charge" },
    el("div", { class: "charge-top" },
      el("div", {},
        el("span", { class: "small muted", text: t("chargeDetail") }),
        by.merchant ? el("span", { class: "merchant", text: by.merchant.value }) : null),
      by.amount
        ? el("div", {},
          el("span", { class: "figure", text: by.amount.value }),
          by.amount.approx ? el("span", { class: "approx", text: by.amount.approx }) : null)
        : null),
    el("div", { class: "detail ruled-top" }, rest.map((row) => el("div", {},
      el("span", { class: "k", text: row.label }),
      el("span", { class: "v", text: row.value })))));
}

// The last turn stays live: switching the language redraws its chips, card and buttons.
function drawTurn(r) {
  const m = turnModel(t, state.lang, r);
  const parts = [];
  if (m.chips.length) {
    parts.push(el("div", { class: "chips" },
      el("span", { class: "small muted", text: `${t("understood")}:` }),
      m.chips.map((chip) => el("span", { class: "chip", title: chip.title },
        el("span", { class: "k", text: chip.label }), el("span", { class: "v", text: chip.value }),
        chip.evidence ? el("span", { class: "evidence", text: `«${chip.evidence}»` }) : null))));
  }
  const text = m.lines.map((line) => (line.simulated
    ? el("span", { class: "sim-label" }, el("span", { class: "sim", text: "simulado" }), line.text)
    : el("span", { class: "line", text: line.text })));
  if (text.length) parts.push(el("div", { class: "bubble" }, text));
  if (m.card.length) parts.push(chargeCard(m.card));
  if (m.question) parts.push(el("div", { class: "bubble" }, el("p", { class: "question", text: m.question })));
  if (m.buttons.length) {
    parts.push(el("div", { class: "choices" }, m.buttons.map((b) => el("button", {
      type: "button", class: `btn${b.primary ? " primary" : ""}`, text: b.label,
      onclick: () => send({ message: b.label, case_id: r.case_id, ...b.answer }, b.label),
    }))));
  }
  if (m.folio) {
    parts.push(el("div", { class: "receipt" },
      el("div", { class: "receipt-top" },
        el("span", { class: "done", text: t("registeredTitle") }),
        el("div", { class: "stack" },
          el("span", { class: "k", text: t("folio") }),
          el("span", { class: "folio", text: m.folio }))),
      el("div", { class: "receipt-bottom" },
        el("div", { class: "check" }, el("div", {}, el("span", { class: "title", text: t("verified") }))),
        el("div", { class: "row" },
          el("a", { class: "btn primary medium", href: "#/aclaraciones", text: t("seeClarifications") }),
          el("a", { class: "btn medium", href: "#/inicio", text: t("navHome") })))));
  }
  parts.push(el("span", { class: "meta", text: `${t("caseLabel")} ${r.case_id} · trace_id ${r.trace_id}` }));
  return parts;
}

function renderTurn(r) {
  $("last-trace").textContent = r.trace_id;
  state.lastTurn = { r, node: bubble("bot", ...drawTurn(r)) };
}

function redrawLastTurn() {
  const last = state.lastTurn;
  if (!last || !last.node.isConnected) return;
  const disabled = [...last.node.querySelectorAll("button")].some((b) => b.disabled);
  last.node.replaceChildren(...drawTurn(last.r));
  if (disabled) for (const b of last.node.querySelectorAll("button")) b.disabled = true;
}

async function send(body, shown) {
  if (state.busy) return;
  state.busy = true;
  const turn = talk.current();
  bubble("me", shown);
  for (const b of $("log").querySelectorAll(".choices button")) b.disabled = true;
  try {
    const r = await api.call("/chat", { method: "POST", body });
    // The customer started a new conversation meanwhile: this answer belongs to the old one.
    if (!talk.isCurrent(turn)) return;
    setCase(r.case_id);
    renderTurn(r);
  } catch (e) {
    if (!talk.isCurrent(turn)) return;
    if (e.code === "case_not_found") setCase(null);
    bubble("bot", el("div", { class: "bubble" }, el("span", { class: "error", text: errorText(t, e) })),
      e.traceId ? el("span", { class: "meta", text: `trace_id ${e.traceId}` }) : null);
  } finally {
    if (talk.isCurrent(turn)) state.busy = false;
  }
}

// A new conversation: no case, an empty log, and nothing of the previous one still arriving.
function newConversation() {
  talk.startNew();
  state.busy = false;
  state.lastTurn = null;
  setCase(null);
  greet();
}

function openByButton(transactionId, text) {
  location.hash = "#/aclarar";
  newConversation();
  send({ message: text, transaction_id: transactionId }, text);
}

// ---- wiring -------------------------------------------------------------------------------

$("message").addEventListener("input", () => {
  $("send").disabled = !canSend($("message").value);
});

$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("message").value.trim();
  if (!canSend(text)) return;
  $("message").value = "";
  $("send").disabled = true;
  const body = { message: text };
  if (state.caseId) body.case_id = state.caseId;
  send(body, text);
});

$("new-case").addEventListener("click", newConversation);

$("cross-access").addEventListener("click", () => {
  // Names someone else in the body: the API ignores it for data and stops the case for
  // security (TRZ-09 CA6). Design 10.3, step 5.
  const text = t("crossAccessMessage");
  newConversation();
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
  state.me = null;
  newConversation();
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

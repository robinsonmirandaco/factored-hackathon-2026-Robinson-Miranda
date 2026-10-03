// Customer web (TRZ-34): login, home, movements, the clarification chat, Mis aclaraciones and,
// in demo mode with the audit view switched on, the trace of a case.
// Every figure on screen is read from the API; the page holds no customer data of its own.
// Text reaches the page only through textContent, never as HTML.

import { createClient } from "./api.js";
import { day, dayMonth, dayTime, label, money, translator } from "./i18n.js";
import {
  AUDIT_KEY, auditOn, auditSwitchView,
  buttonMessage, canSend, clarificationLines, closedNote, codeStep, composerState, createConversation,
  deadlineKind, traceCase, traceHref, traceLines,
  errorText,
  infoRequestViews, movementDetail, openQuestions, reviewLine, statusKey, statusTone, turnModel,
  unreadBadge,
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
  lastTurn: null,
  notes: null,
  audit: auditOn(api.store.read(AUDIT_KEY)),
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
  drawAudit();
  route();
}

function showChrome(loggedIn) {
  $("appbar").hidden = !loggedIn;
  $("clock").hidden = !loggedIn || !state.me;
  $("avatar-initial").textContent = (state.me?.first_name || "").slice(0, 1).toUpperCase();
  if (!loggedIn) {
    state.notes = null;
    togglePanel(false);
    $("notif-dot").hidden = true;
  }
}

// ---- notifications (TRZ-32) ---------------------------------------------------------------

// The counter on the avatar, read again on every view and when the window gets focus, so a
// decision of the analyst shows up without asking.
async function refreshNotifications() {
  if (!api.hasSession()) return;
  try {
    state.notes = await api.call("/me/notifications");
  } catch {
    return;
  }
  const badge = unreadBadge(t, state.notes.unread);
  const dot = $("notif-dot");
  dot.hidden = badge.hidden;
  dot.textContent = badge.text;
  $("avatar").setAttribute("aria-label", `${t("notifications")}: ${badge.title}`);
  $("avatar").title = badge.title;
  if (!$("notif-panel").hidden) drawNotifications();
}

function drawNotifications() {
  const items = state.notes?.items || [];
  $("notif-panel").replaceChildren(
    el("h2", { text: t("notifications") }),
    items.length
      ? el("ul", { class: "notif-list" }, items.map((n) => el("li", {},
        el("button", { type: "button", class: `notif${n.read ? "" : " unread"}`, onclick: () => openNotification(n) },
          el("span", { text: n.text }),
          n.read ? null : el("span", { class: "tag new", text: t("notificationNew") })))))
      : el("p", { class: "small muted", text: t("noNotifications") }));
}

function togglePanel(open) {
  const panel = $("notif-panel");
  panel.hidden = !open;
  $("avatar").setAttribute("aria-expanded", String(open));
  if (open) drawNotifications();
}

// Opening a notification marks it read and takes the customer to the clarification it is about.
async function openNotification(n) {
  if (!n.read) {
    try {
      await api.call(`/me/notifications/${encodeURIComponent(n.id)}/read`, { method: "POST" });
    } catch {
      // It stays unread and is offered again; Mis aclaraciones still shows the decision.
    }
  }
  togglePanel(false);
  if (location.hash === "#/aclaraciones") route();
  else location.hash = "#/aclaraciones";
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
  const info = el("p", { class: "muted small", "aria-live": "polite" });
  const error = el("p", { class: "error", role: "alert" });
  const askButton = el("button", { type: "button", class: "btn primary block", text: t("requestCode") });
  const enterButton = el("button", { type: "submit", class: "btn primary block", text: t("verify") });
  const changeButton = el("button", { type: "button", class: "link small", text: t("changeDocument") });
  const doc = () => ({ document_type: type.value, document_number: number.value.trim() });
  let requestedFor = null;

  // Two steps, as in the prototype: the document, then the code that was sent for it.
  const docField = el("div", {},
    el("label", { for: number.id, text: t("documentLabel") }),
    el("div", { class: "doc-field" }, type, number));
  const docStep = el("div", { class: "stack login-step" }, docField, askButton,
    el("p", { class: "login-note", text: t("loginNote") }));
  const codeField = el("div", {},
    el("div", { class: "label-row" }, el("label", { for: code.id, text: t("code") }), changeButton),
    code);
  const codeStepBlock = el("div", { class: "stack login-step" }, codeField, info,
    el("p", { class: "notice", text: t("identityNote") }), enterButton);

  // A code belongs to the document it was asked for: changing the document hides the field.
  const sync = () => {
    const { showCode } = codeStep(requestedFor, { type: type.value, number: number.value.trim() });
    docStep.hidden = showCode;
    codeStepBlock.hidden = !showCode;
    code.disabled = !showCode;
    if (!showCode) code.value = "";
    (showCode ? codeField : docField).after(error);
  };
  type.addEventListener("change", () => { requestedFor = null; sync(); });
  number.addEventListener("input", sync);
  changeButton.addEventListener("click", () => {
    requestedFor = null;
    error.textContent = "";
    sync();
    number.focus();
  });

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
    if (codeStepBlock.hidden) return askButton.click();
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

  form.append(docStep, codeStepBlock);
  sync();
}

async function loadMe() {
  state.me = await api.call("/me");
  $("audit").hidden = !state.me.demo;
  applyTexts();
  showChrome(true);
  drawAudit();
}

// ---- the audit view of the demo (TRZ-34 CA4) ----------------------------------------------

// The trace is shown only with the switch on, in demo mode, labeled "demo" by its panel.
const auditVisible = () => state.audit && Boolean(state.me?.demo);

function timeline(steps) {
  const lines = traceLines(steps);
  if (!lines.length) return el("p", { class: "small muted", text: t("traceEmpty") });
  return el("ol", { class: "timeline" }, lines.map((l) => el("li", {},
    el("span", { class: "rail" }),
    el("div", { class: "body" },
      el("span", { class: "title", text: l.text }),
      el("span", { class: "when", text: l.meta })))));
}

const traceOf = (caseId) => api.call(
  `/me/clarifications/${encodeURIComponent(caseId)}/trace?lang=${encodeURIComponent(state.lang)}`);

function drawAudit() {
  const view = auditSwitchView(t, state.audit);
  $("audit-switch").textContent = view.text;
  $("audit-switch").setAttribute("aria-checked", String(view.checked));
  $("audit-trace").hidden = !auditVisible();
  loadPanelTrace();
}

// The trace of the case of the conversation, read again after every turn.
async function loadPanelTrace() {
  const target = $("audit-trace");
  if (!auditVisible()) return target.replaceChildren();
  const caseId = state.caseId;
  if (!caseId) return target.replaceChildren(el("p", { class: "small muted", text: t("traceNoCase") }));
  try {
    const steps = await traceOf(caseId);
    if (state.caseId === caseId && auditVisible()) target.replaceChildren(timeline(steps));
  } catch (e) {
    target.replaceChildren(el("p", { class: "small error", text: errorText(t, e) }));
  }
}

async function renderTrace(caseId) {
  const target = $("view-trace");
  try {
    const steps = await traceOf(caseId);
    target.replaceChildren(
      el("div", { class: "page-head" },
        el("a", { class: "back", href: "#/aclaraciones", text: t("backClarifications") }),
        el("div", { class: "row" }, el("h1", { text: t("traceTitle") }), el("span", { class: "sim", text: "demo" }))),
      el("section", { class: "card" },
        el("span", { class: "tiny muted mono", text: `${t("caseLabel")} ${caseId}` }),
        el("p", { class: "small muted", text: t("traceNote") }),
        timeline(steps)));
  } catch (e) {
    failure(target, e);
  }
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
  const traced = traceCase(location.hash);
  if (traced) {
    if (!auditVisible()) {
      location.hash = "#/aclaraciones";
      return;
    }
    show("trace");
    refreshNotifications();
    return renderTrace(traced);
  }
  const view = VIEWS[location.hash] || "home";
  show(view);
  refreshNotifications();
  if (view === "home") renderHome();
  if (view === "movements") renderMovements(true);
  if (view === "clarifications") renderClarifications();
}

function show(view) {
  for (const name of ["login", "home", "movements", "chat", "clarifications", "trace"]) {
    $(`view-${name}`).hidden = name !== view;
  }
  for (const a of document.querySelectorAll("#tabs a")) {
    if (a.dataset.view === view || (view === "trace" && a.dataset.view === "clarifications")) {
      a.setAttribute("aria-current", "page");
    } else a.removeAttribute("aria-current");
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
  const key = statusKey(c);
  return el("span", { class: `pill ${statusTone(key)}`, text: label(t, "status", key) });
}

const SVG = "http://www.w3.org/2000/svg";

// The contactless mark of the prototype's card, drawn as DOM nodes (no inline markup).
function contactless() {
  const svg = document.createElementNS(SVG, "svg");
  for (const [k, v] of Object.entries({
    width: "22", height: "22", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "1.8", "stroke-linecap": "round", class: "vcard-wave", "aria-hidden": "true",
  })) svg.setAttribute(k, v);
  for (const d of ["M8.5 8.5a5 5 0 0 1 0 7", "M12 6a8.5 8.5 0 0 1 0 12", "M15.5 3.5a12 12 0 0 1 0 17"]) {
    const path = document.createElementNS(SVG, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

// A card drawn with what the record has: the kind, the last four digits and the holder's first
// name. No expiry, number or limit bar is drawn, since the API has none of them.
function virtualCard(p) {
  const credit = p.product_type === "credit_card";
  return el("div", { class: `vcard ${credit ? "credit" : "debit"}`, "aria-hidden": "true" },
    el("div", { class: "vcard-row" },
      el("span", { class: "vcard-brand" }, el("span", { class: "vcard-logo", text: "L" }), "LATAM Bank"),
      el("span", { class: "vcard-kind", text: t(`cardKind_${p.product_type}`) })),
    el("div", { class: "vcard-row start" }, el("span", { class: "vcard-chip" }), contactless()),
    el("div", { class: "vcard-foot" },
      el("span", { class: "vcard-number", text: `•••• •••• •••• ${p.last4}` }),
      el("span", { class: "vcard-holder", text: (state.me.first_name || "").toUpperCase() })));
}

function productSection(p) {
  const drawn = p.last4 && (p.product_type === "credit_card" || p.product_type === "debit_card");
  return el("section", { class: "card" },
    el("div", { class: "card-head" },
      el("h2", { text: label(t, "product", p.product_type) }),
      el("span", {
        class: `pill ${p.status === "Active" ? "ok" : p.status === "Blocked" ? "bad" : ""}`,
        text: label(t, "pstatus", p.status),
      })),
    drawn ? virtualCard(p) : p.last4 ? el("span", { class: "mono small muted", text: `•••• ${p.last4}` }) : null,
    p.current_balance != null || p.credit_limit != null
      ? el("div", { class: "figures" },
        p.current_balance != null
          ? [el("span", { class: "small muted", text: t("balance") }),
            el("span", { class: "figure", text: money(state.lang, p.current_balance, p.currency) })]
          : null,
        p.credit_limit != null
          ? el("span", { class: "small muted", text: `${t("limit")}: ${money(state.lang, p.credit_limit, p.currency)}` })
          : null)
      : null);
}

function recentSection(items) {
  const rows = items.map((m) => {
    const what = m.merchant || label(t, "type", m.transaction_type);
    return el("li", { class: "recent-row" },
      el("span", { class: "initial", text: initialOf(what) }),
      el("span", { class: "main" },
        el("span", { class: "title", text: what }),
        el("span", { class: "sub", text: [dayMonth(state.lang, m.at), label(t, "channel", m.channel)].filter(Boolean).join(" · ") })),
      el("span", { class: "end" }, amountCell(m),
        m.status === "Pending" ? el("span", { class: "pill warn", text: label(t, "tx", m.status) }) : null));
  });
  return el("section", { class: "card recent" },
    el("div", { class: "card-head recent-head" },
      el("h2", { text: t("recentMovements") }),
      el("a", { class: "link", href: "#/movimientos", text: t("seeAll") })),
    rows.length ? el("ul", { class: "list" }, rows) : el("p", { class: "muted small recent-empty", text: t("noMovements") }));
}

async function renderHome() {
  const target = $("view-home");
  try {
    const [products, clarifications, recent] = await Promise.all([
      api.call("/me/products"), api.call("/me/clarifications"), api.call("/me/transactions?limit=5"),
    ]);
    const questions = markQuestions(clarifications);
    const name = state.me.first_name;
    const claims = clarifications.slice(0, 3).map((c) => {
      const { title, ref } = clarificationLines(t, state.lang, c);
      return el("div", { class: "claim-mini" },
        el("div", {},
          el("span", { class: "small", text: title }),
          ref ? el("span", { class: "tiny muted mono", text: ref }) : null),
        statusPill(c));
    });
    const [first, ...others] = products.map(productSection);
    target.replaceChildren(
      el("div", { class: "page-head" },
        el("span", { class: "eyebrow", text: day(state.lang, state.me.now) }),
        el("h1", { text: name ? t("hello", { name }) : t("helloAnon") })),
      el("div", { class: "grid-2" },
        el("div", { class: "stack-gap" },
          first || el("section", { class: "card" }, el("p", { class: "muted", text: t("noProducts") })),
          recentSection(recent.items)),
        el("div", { class: "stack-gap" },
          others,
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
            claims.length ? el("div", {}, claims) : el("p", { class: "muted small", text: t("noClarifications") })))),
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
          auditVisible() && c.case_id
            ? el("div", {}, el("a", { class: "btn small", href: traceHref(c.case_id), text: t("seeTrace") }))
            : null,
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
  const node = el("div", { class: "msg bot" }, el("span", { class: "bot-mark", "aria-hidden": "true", text: "L" }), turn);
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
  loadPanelTrace();
}

function redrawLastTurn() {
  const last = state.lastTurn;
  if (!last || !last.node.isConnected) return;
  const disabled = [...last.node.querySelectorAll("button")].some((b) => b.disabled);
  last.node.replaceChildren(...drawTurn(last.r));
  if (disabled) for (const b of last.node.querySelectorAll("button")) b.disabled = true;
}

// Send and Enter are off while a request is in flight, and the reply is shown on its way.
function drawComposer() {
  const view = composerState($("message").value, talk.busy());
  $("send").disabled = !view.canSubmit;
  $("pending").hidden = !view.pending;
}

// One request at a time: a message, an option or a confirmation pressed while another one is in
// flight is not sent. `sent` runs once the API answered, so typed text is cleared only then.
async function send(body, shown, sent) {
  const turn = talk.begin();
  if (turn === null) return;
  bubble("me", shown);
  for (const b of $("log").querySelectorAll(".choices button")) b.disabled = true;
  drawComposer();
  try {
    const r = await api.call("/chat", { method: "POST", body });
    // The customer started a new conversation meanwhile: this answer belongs to the old one.
    if (!talk.end(turn, true)) return;
    sent?.();
    setCase(r.case_id);
    renderTurn(r);
  } catch (e) {
    talk.end(turn, false);
    if (!talk.isCurrent(turn)) return;
    if (e.code === "case_not_found") setCase(null);
    bubble("bot", el("div", { class: "bubble" }, el("span", { class: "error", text: errorText(t, e) })),
      e.traceId ? el("span", { class: "meta", text: `trace_id ${e.traceId}` }) : null);
  } finally {
    drawComposer();
  }
}

// A new conversation: no case, an empty log, and nothing of the previous one still arriving.
function newConversation() {
  talk.startNew();
  state.lastTurn = null;
  setCase(null);
  greet();
  drawComposer();
  loadPanelTrace();
}

function openByButton(transactionId, text) {
  location.hash = "#/aclarar";
  newConversation();
  send({ message: text, transaction_id: transactionId }, text);
}

// ---- wiring -------------------------------------------------------------------------------

$("message").addEventListener("input", drawComposer);

$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("message").value.trim();
  // Enter while a request is in flight does nothing, and the text stays in the box.
  if (!composerState(text, talk.busy()).canSubmit) return;
  const body = { message: text };
  if (state.caseId) body.case_id = state.caseId;
  send(body, text, () => {
    // Only what was sent is cleared, not what the customer typed while waiting.
    if ($("message").value.trim() === text) $("message").value = "";
  });
});

$("new-case").addEventListener("click", newConversation);

$("audit-switch").addEventListener("click", () => {
  state.audit = !state.audit;
  api.store.write(AUDIT_KEY, state.audit ? "on" : null);
  drawAudit();
});

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

$("avatar").addEventListener("click", (event) => {
  event.stopPropagation();
  togglePanel($("notif-panel").hidden);
});
$("notif-panel").addEventListener("click", (event) => event.stopPropagation());
document.addEventListener("click", () => togglePanel(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") togglePanel(false);
});
window.addEventListener("focus", refreshNotifications);

window.addEventListener("hashchange", route);
applyTexts();
greet();
route();

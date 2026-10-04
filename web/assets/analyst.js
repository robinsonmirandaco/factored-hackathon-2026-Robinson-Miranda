// Analyst console (TRZ-27, TRZ-28, TRZ-29, TRZ-31, TRZ-35, TRZ-38): login, the queue with its
// filters, the dossier of a case, the decisions, the Estado de autonomía tab, and the automation
// switch and demo reset in the header.
// Every figure is read from the API; text reaches the page only through textContent, never as
// HTML.

import { createClient } from "./api.js";
import {
  INJECTION_REASON, REVERSAL_REASONS, SIMULATED_NOTE, actionLabel, autonomyRow, reversedGroups, thresholdsText, actionStateLabel, auditLabel, automationView, demoResetView,
  identificationTable, recommendationText, cellEvidence,
  caseHeading, clueChips, dialogKey, decisionDone, decisionPanel, decisionProblem, factRows, filterChips,
  historyTurns, identificationLabel, infoExchanges, kindLabel, markedParts, queueRow, reasonLabel, statusLabel,
} from "./analyst-view.js";

const api = createClient("analyst");
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

const ERRORS = {
  invalid_credentials: "Usuario o contraseña incorrectos.",
  not_authenticated: "La sesión terminó. Vuelve a entrar.",
  session_expired: "La sesión terminó. Vuelve a entrar.",
  forbidden: "Esta sesión no es de analista.",
  case_not_found: "El caso no existe.",
  case_not_escalated: "El caso ya no está en la cola.",
  nothing_to_approve: "No hay acción que ejecutar: pide información o rechaza.",
  charge_already_disputed: "El cargo ya tiene una aclaración abierta.",
  decision_not_allowed: "Esta decisión no está disponible para este caso.",
  reason_required: "Elige un motivo de la lista.",
  reason_not_allowed: "El motivo no está en la lista.",
  question_required: "Escribe la pregunta para el cliente.",
  db_unavailable: "La base de datos no responde. Intenta de nuevo.",
  demo_not_seeded: "Esta base no tiene el estado del demo: corre make seed-demo.",
  demo_script_diverged: "Un caso sembrado no terminó como espera config/demo.yaml; nada cambió.",
};
const errorText = (e) => ERRORS[e?.code] || "Algo falló. Intenta de nuevo.";

function failure(target, e) {
  target.replaceChildren(el("div", { class: "card" },
    el("p", { class: "error", text: errorText(e) }),
    e?.traceId ? el("p", { class: "muted small mono", text: `trace_id ${e.traceId}` }) : null));
}

const pill = (p) => el("span", { class: `pill${p.tone ? ` ${p.tone}` : ""}`, text: p.text });

// ---- session ------------------------------------------------------------------------------

function showChrome(loggedIn) {
  $("appbar").hidden = !loggedIn;
  if (loggedIn) {
    loadAutomation();
    loadDemo();
  }
}

// ---- global automation switch (TRZ-35) ----------------------------------------------------

let automation = null;

function drawAutomation() {
  const button = $("automation");
  const view = automationView(automation);
  button.textContent = view.text;
  button.className = `pill switch ${view.tone}`;
  button.title = view.action;
  button.hidden = automation === null;
}

async function loadAutomation() {
  try {
    automation = await api.call("/automation");
  } catch {
    automation = null;
  }
  drawAutomation();
}

// The header's dialog, with the markup and style of the customer's expired session dialog: the
// automation switch and the demo reset ask through it. The focus goes into it, Tab stays on its
// buttons, Escape or Cancelar closes it unchanged, and the focus goes back to the button that
// opened it.
let dialog = null;

function openDialog(view, run, opener) {
  dialog = { run, opener };
  $("switch-title").textContent = view.title;
  $("switch-text").textContent = view.confirm;
  $("switch-cancel").textContent = view.cancelLabel;
  $("switch-confirm").textContent = view.confirmLabel;
  $("switch-confirm").disabled = false;
  $("switch-error").hidden = true;
  $("switch-dialog").hidden = false;
  $("switch-cancel").focus();
}

function closeDialog() {
  $("switch-dialog").hidden = true;
  dialog?.opener.focus();
  dialog = null;
}

$("automation").addEventListener("click", () => openDialog(automationView(automation), async () => {
  automation = await api.call("/automation", {
    method: "PUT", body: { all_to_human: !automation.all_to_human },
  });
  drawAutomation();
}, $("automation")));

// ---- demo reset (TRZ-38) ------------------------------------------------------------------

async function loadDemo() {
  try {
    const state = await api.call("/demo");
    $("demo-reset").hidden = !state.seeded;
  } catch {
    // Outside demo mode the route does not exist.
    $("demo-reset").hidden = true;
  }
}

$("demo-reset").addEventListener("click", () => {
  // One key per dialog: a second click on the same dialog does not reset twice.
  const key = crypto.randomUUID();
  openDialog(demoResetView(), async () => {
    await api.call("/demo/reset", { method: "POST", headers: { "idempotency-key": key } });
    filter = "all";
    await loadAutomation();
    location.hash = "#/cola";
    route();
  }, $("demo-reset"));
});

$("switch-cancel").addEventListener("click", closeDialog);

$("switch-confirm").addEventListener("click", async () => {
  const button = $("switch-confirm");
  button.disabled = true;
  try {
    await dialog.run();
    closeDialog();
  } catch (e) {
    $("switch-error").textContent = errorText(e);
    $("switch-error").hidden = false;
    button.disabled = false;
  }
});

$("switch-dialog").addEventListener("keydown", (event) => {
  const buttons = [$("switch-cancel"), $("switch-confirm")].filter((b) => !b.disabled);
  const step = dialogKey(event.key, event.shiftKey, buttons.indexOf(document.activeElement), buttons.length);
  if (!step) return;
  event.preventDefault();
  if (step.cancel) closeDialog();
  else buttons[step.focus].focus();
});

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("login-error");
  error.hidden = true;
  try {
    const out = await api.call("/auth/analyst/login", {
      method: "POST",
      body: { username: $("username").value.trim(), password: $("password").value },
    });
    api.setToken(out.access_token);
    $("password").value = "";
    location.hash = "#/cola";
    route();
  } catch (e) {
    error.textContent = errorText(e);
    error.hidden = false;
  }
});

$("logout").addEventListener("click", async () => {
  try {
    await api.call("/auth/logout", { method: "POST" });
  } catch {
    // The session ends here either way.
  }
  api.clear();
  route();
});

window.addEventListener("trazo:session-lost", () => {
  api.clear();
  route();
});

// ---- views --------------------------------------------------------------------------------

let filter = "all";

function show(view) {
  for (const name of ["login", "queue", "case", "autonomy"]) $(`view-${name}`).hidden = name !== view;
  for (const a of document.querySelectorAll("#nav a")) {
    if (a.dataset.view === view || (view === "case" && a.dataset.view === "queue")) {
      a.setAttribute("aria-current", "page");
    } else a.removeAttribute("aria-current");
  }
}

function route() {
  if (!api.hasSession()) {
    showChrome(false);
    return show("login");
  }
  showChrome(true);
  const match = location.hash.match(/^#\/caso\/(.+)$/);
  if (match) {
    show("case");
    return renderCase(decodeURIComponent(match[1]));
  }
  if (location.hash === "#/autonomia") {
    show("autonomy");
    return renderAutonomy();
  }
  show("queue");
  return renderQueue();
}

const QUEUE_COLUMNS = ["Caso", "Cliente", "Tipo", "Monto USD", "Idioma", "Motivo", "Prioridad", "SLA"];

async function renderQueue() {
  const target = $("view-queue");
  try {
    const params = filter === "all" ? "" : `?filter=${encodeURIComponent(filter)}`;
    const queue = await api.call(`/queue${params}`);
    const now = Date.now();
    const rho = queue.audit_sample_rate;
    const chips = filterChips(queue.counts, filter).map((c) => el("button", {
      type: "button",
      class: "pick",
      "aria-pressed": String(c.pressed),
      onclick: () => { filter = c.key; renderQueue(); },
    }, el("span", { text: c.label }), el("span", { class: "count", text: String(c.count) })));
    const rows = queue.items.map((item) => {
      const r = queueRow(item, now, rho);
      return el("a", { class: "trow", href: r.href },
        el("span", { class: "cell-stack" },
          el("span", { class: "mono small", text: r.caseId }),
          r.tags.map((tag) => el("span", { class: `tag ${tag.tone}`, text: tag.text }))),
        el("span", { class: "cell-stack" },
          el("span", { class: "strong", text: r.customer }),
          el("span", { class: "tiny muted", text: r.kind })),
        el("span", { text: r.type }),
        el("span", { class: "r strong", text: r.amount ?? "" }),
        el("span", { class: "mono tiny", text: r.language }),
        el("span", { class: "reason", text: r.reason }),
        pill(r.priority),
        el("span", { class: `r strong sla ${r.sla.tone}`, text: r.sla.text }));
    });
    target.replaceChildren(
      el("div", { class: "queue-head" },
        el("div", { class: "page-head" },
          el("span", { class: "eyebrow", text: "Operación" }),
          el("h1", { text: "Cola de casos" })),
        el("div", { class: "stats" },
          el("div", {}, el("span", { class: "stat", text: String(queue.counts?.all ?? 0) }), el("span", { class: "small muted", text: "En cola" })),
          el("div", {}, el("span", { class: "stat", text: String(queue.counts?.high_priority ?? 0) }), el("span", { class: "small muted", text: "Alta prioridad" })))),
      el("div", { class: "pills queue-filters" }, chips),
      el("section", { class: "card flush table-wrap queue" },
        el("div", { class: "table" },
          el("div", { class: "trow head" }, QUEUE_COLUMNS.map((c, i) => el("span", { class: i === 3 || i === 7 ? "r" : null, text: c }))),
          rows.length ? rows : el("div", { class: "empty", text: "No hay casos en este filtro." }))),
      el("p", { class: "small muted note-below", text: "El SLA corre en el reloj real, según la prioridad. Los montos en USD son los que comparó la política." }));
  } catch (e) {
    failure(target, e);
  }
}

const AUTONOMY_COLUMNS = ["Celda", "Nivel", "Revisiones del bloque", "Reversiones", "r", "W", "Último cambio", ""];

// Read again every time the tab is opened or the page reloaded, so a decision of the analyst
// shows up at once (CA4).
async function renderAutonomy() {
  const target = $("view-autonomy");
  target.replaceChildren(el("p", { class: "muted", text: "Cargando el estado de autonomía…" }));
  try {
    const tab = await api.call("/autonomy");
    const rows = tab.cells.map((cell) => {
      const r = autonomyRow(cell, tab.thresholds);
      const cases = el("div", { class: "reversed", hidden: true }, reversedGroups(cell).map((g) =>
        el("div", { class: "stack tight-stack" },
          el("span", { class: "small strong", text: g.title }),
          g.items.length
            ? el("ul", { class: "list" }, g.items.map((i) => el("li", { class: "row reversed-row" },
              el("a", { class: "mono small", href: i.href, text: i.caseId }),
              el("span", { class: "small", text: i.reason }),
              i.simulated ? el("span", { class: "tag sim", text: "[simulado]" }) : null)))
            : el("p", { class: "small muted", text: "Sin casos revertidos." }))));
      const toggle = el("button", {
        type: "button", class: "btn small", "aria-expanded": "false", disabled: r.reversedCount === 0,
        text: `Ver casos (${r.reversedCount})`,
      });
      toggle.addEventListener("click", () => {
        cases.hidden = !cases.hidden;
        toggle.setAttribute("aria-expanded", String(!cases.hidden));
      });
      return el("div", { class: "cell-block" },
        el("div", { class: "trow" },
          el("span", { class: "strong", text: r.cell }),
          el("span", { class: "cell-stack" },
            el("span", { class: "strong mono", text: r.level }),
            el("span", { class: "tiny muted", text: r.levelText })),
          el("span", { class: "r", text: r.reviews }),
          el("span", { class: "r", text: r.reversals }),
          el("span", { class: "r", text: r.rate }),
          el("span", { class: "cell-stack r" },
            el("span", { text: r.w }),
            r.wNote ? el("span", { class: "tiny muted", text: r.wNote }) : null),
          el("span", { class: "cell-stack" },
            el("span", { class: "small", text: r.change }),
            r.changeCase ? el("a", { class: "tiny mono", href: `#/caso/${encodeURIComponent(r.changeCase)}`, text: `Cerró el bloque ${r.changeCase}` }) : null),
          toggle),
        cases);
    });
    target.replaceChildren(
      el("div", { class: "page-head" },
        el("span", { class: "eyebrow", text: "Supervisión" }),
        el("h1", { text: "Estado de autonomía" })),
      el("div", { class: "pills thresholds" }, thresholdsText(tab.thresholds).map((text) => el("span", { class: "pill", text }))),
      el("section", { class: "card flush table-wrap autonomy" },
        el("div", { class: "table" },
          el("div", { class: "trow head" }, AUTONOMY_COLUMNS.map((c, i) => el("span", { class: i >= 2 && i <= 5 ? "r" : null, text: c }))),
          rows)),
      tab.simulated
        ? el("p", { class: "small muted note-below" }, el("span", { class: "sim", text: "[simulado]" }), ` ${SIMULATED_NOTE}`)
        : null,
      el("p", { class: "small muted", text: "Las revisiones de cada celda se agrupan en bloques de N, en el orden en que decide la analista; W se calcula solo al cerrar un bloque." }));
  } catch (e) {
    failure(target, e);
  }
}

async function renderCase(caseId) {
  const target = $("view-case");
  target.replaceChildren(el("p", { class: "muted", text: "Cargando el expediente…" }));
  try {
    const [one, dossier, history, queue] = await Promise.all([
      api.call(`/cases/${encodeURIComponent(caseId)}`),
      api.call(`/cases/${encodeURIComponent(caseId)}/dossier`),
      api.call(`/cases/${encodeURIComponent(caseId)}/history`),
      api.call("/queue"),
    ]);
    const item = queue.items.find((i) => i.case_id === caseId) || null;
    const row = item ? queueRow(item, Date.now(), queue.audit_sample_rate) : null;
    const header = el("section", { class: "card" },
      el("div", { class: "card-head start" },
        el("div", { class: "stack tight-stack" },
          el("span", { class: "mono small muted", text: caseHeading(dossier.case_kind, one.status, dossier.case_id) }),
          el("h1", { class: "case-title", text: dossier.request_summary })),
        el("div", { class: "row" },
          dossier.simulated ? el("span", { class: "sim", text: "[simulado]" }) : null,
          row ? pill({ text: `Prioridad ${row.priority.text}`, tone: row.priority.tone }) : null,
          pill({ text: statusLabel(one.status), tone: STATUS_TONE[one.status] || "" }))),
      el("div", { class: "detail ruled-top" },
        field("Tipo", kindLabel(dossier.case_kind)),
        field("Idioma", (dossier.language || "?").toUpperCase()),
        row?.amount ? field("Monto USD", row.amount) : null,
        row ? field("SLA", row.sla.text) : null,
        field("trace_id", dossier.trace_id, "mono")));
    target.replaceChildren(
      el("div", { class: "case" },
        el("a", { class: "back", href: "#/cola", text: "← Cola de casos" }),
        header,
        dossierCards(dossier, history),
        decisionCard(dossier, item, one.status)));
  } catch (e) {
    failure(target, e);
  }
}

const STATUS_TONE = {
  approved: "ok", rejected: "", awaiting_customer: "warn", security_blocked: "bad", failed: "bad",
};

function field(k, v, cls) {
  return el("div", {}, el("span", { class: "k", text: k }), el("span", { class: `v${cls ? ` ${cls}` : ""}`, text: v }));
}

function dossierCards(d, history) {
  const cards = [];
  if (d.audit_draw) cards.push(auditCard(d.audit_draw));
  if (d.original_message) {
    cards.push(el("section", { class: "card tight" },
      el("h2", { text: "Mensaje original" }),
      el("div", { class: "message" },
        el("p", { class: "original" }, markedParts(d.original_message, d.injected_spans).map((p) =>
          p.marked ? el("mark", { class: "injected", title: "Instrucción inyectada: no se obedeció", text: p.text }) : p.text)),
        d.machine_translation
          ? el("div", { class: "translation" },
            el("span", { class: "sim", text: `Traducción automática${d.machine_translation.model ? ` · ${d.machine_translation.model}` : ""}` }),
            el("p", { text: d.machine_translation.text || "La traducción no está disponible." }))
          : null)));
  }
  const chips = clueChips(d.extraction);
  if (chips.length) {
    cards.push(el("section", { class: "card tight" },
      el("h2", { text: "Lo que se entendió" }),
      el("div", { class: "chips" }, chips.map((c) => el("span", { class: "chip", title: c.evidence },
        el("span", { class: "k", text: c.label }), el("span", { class: "v", text: c.value })))),
      el("p", { class: "small muted", text: "Cada pista trae su fragmento literal del mensaje (pasa el cursor)." })));
  }
  const facts = factRows(d.verified_facts);
  if (facts.length) cards.push(factCard("Hechos verificados", facts));
  if (identificationTable(d.identification).rows.length) cards.push(identificationCard(d.identification));
  const evidence = factRows(d.evidence);
  if (evidence.length) cards.push(factCard("Evidencia", evidence));
  if (d.later_messages.length) {
    cards.push(el("section", { class: "card tight" },
      el("h2", { text: "Mensajes después del paso a una persona" }),
      d.later_messages.map((m) => el("div", { class: "message" },
        el("p", { class: "original", text: m.text }),
        el("span", { class: "tiny muted mono", text: `${m.source.table} · ${m.source.id}` })))));
  }
  const exchanges = infoExchanges(d.info_exchanges);
  if (exchanges.length) {
    cards.push(el("section", { class: "card tight" },
      el("h2", { text: "Preguntas al cliente y respuestas" }),
      el("ol", { class: "exchanges" }, exchanges.map((x) => el("li", { class: "message" },
        el("span", { class: "tiny muted", text: x.meta }),
        el("p", { class: "question", text: x.question }),
        el("p", { class: x.answered ? "answer" : "answer muted", text: x.answer }),
        el("span", { class: "tiny muted mono", text: x.source }))))));
  }
  const side = [actionsCard(d), ruleCard(d)].filter(Boolean);
  if (side.length) cards.push(el("div", { class: "grid-2 pair" }, side));
  if (d.open_questions.length) {
    cards.push(el("section", { class: "card tight" },
      el("h2", { text: "Preguntas abiertas" }),
      d.open_questions.map((q) => el("p", { class: "dotline", text: q.text }))));
  }
  cards.push(el("section", { class: "card tight" },
    el("h2", { text: "Historial" }),
    historyTurns(history).map((turn) => el("section", { class: "trace-turn" },
      el("h3", { class: "trace-turn-head", text: `Turno ${turn.number} · ${turn.header}` }),
      el("ol", { class: "timeline" }, turn.steps.map((s) => el("li", {},
        el("span", { class: "rail" }),
        el("div", { class: "body" },
          el("span", { class: "title" }, el("span", { class: "step-no", text: `${s.number}.` }), s.text),
          el("span", { class: "when", text: [s.meta, s.time, s.duration && `(${s.duration})`].filter(Boolean).join(" · ") })))))))));
  return cards;
}

// Why the case is here although the system resolved it: the draw, which anyone can recompute
// from its seed and its number (TRZ-29).
function auditCard(draw) {
  const u = Number(draw.u).toFixed(4).replace(".", ",");
  return el("section", { class: "card tight" },
    el("div", { class: "card-head" },
      el("h2", { text: auditLabel(draw.rho) }),
      pill({ text: "Resuelto por el sistema", tone: "info" })),
    el("p", { class: "small", text: `El sistema registró y verificó esta aclaración sin una persona. Salió en el sorteo n.º ${draw.n} con u = ${u} < ρ.` }),
    el("span", { class: "tiny muted mono", text: `semilla ${draw.seed} · ${draw.source.table} · ${draw.source.id}` }));
}

function factCard(title, rows) {
  return el("section", { class: "card tight" },
    el("h2", { text: title }),
    el("div", { class: "checks" }, rows.map((r) => el("div", { class: "check" }, el("div", {},
      el("span", { class: "title", text: `${r.label}: ${r.value}` }),
      el("span", { class: "src", text: r.source }))))));
}

// Candidates with the contribution of each part of the score and the probability; percentages
// are for the analyst only (rule 3).
function identificationCard(identification) {
  const table = identificationTable(identification);
  const columns = `minmax(150px, 1.6fr) repeat(${table.header.length - 1}, 72px)`;
  // Cells come as one flat list: el() flattens one level, so a nested list would be printed.
  const line = (cells, cls) => {
    const node = el("div", { class: `trow${cls ? ` ${cls}` : ""}` }, ...cells);
    node.style.gridTemplateColumns = columns;
    return node;
  };
  const right = (text, cls) => el("span", { class: cls ? `r ${cls}` : "r", text });
  return el("section", { class: "card tight" },
    el("div", { class: "card-head" },
      el("h2", { text: "Identificación" }),
      pill({ text: identificationLabel(identification.decision), tone: "info" })),
    el("p", { class: "small muted", text: `${identification.candidates} candidatos · el conjunto conformal va marcado` }),
    el("div", { class: "table-wrap boxed" },
      el("div", { class: "table" },
        line(table.header.map((text, i) => (i ? right(text) : el("span", { text }))), "head"),
        table.rows.map((row) => line([
          el("span", { class: "cell-stack" },
            el("span", { class: "mono small", text: row.id }),
            row.inSet ? el("span", { class: "tag info", text: "En el conjunto" }) : null),
          ...row.cells.map((text, i) => right(text, i === row.cells.length - 1 ? "strong" : "")),
        ], row.inSet ? "in-set" : null)))));
}

function actionsCard(d) {
  if (!d.actions_taken.length) return null;
  return el("section", { class: "card tight" },
    el("h2", { text: "Acciones del caso" }),
    el("ul", { class: "list" }, d.actions_taken.map((a) => el("li", { class: "row action-row" },
      el("span", { text: actionLabel(a.action) }), el("span", { class: "spacer" }),
      el("span", {
        class: `pill ${a.state === "verified" ? "ok" : a.state === "failed" ? "bad" : "warn"}`,
        text: actionStateLabel(a.state),
      })))));
}

function ruleCard(d) {
  const rule = d.policy_rule_triggered;
  if (!rule) return null;
  return el("section", { class: "card tight" },
    el("h2", { text: d.case_kind === "audit_sample" ? "Regla que decidió" : "Regla que escaló" }),
    el("span", { class: "rule mono", text: rule.rule }),
    el("span", { text: d.injection ? INJECTION_REASON : reasonLabel(rule.rule) }),
    el("span", { class: "tiny muted mono", text: `política ${rule.version} · nivel ${rule.level}${rule.autonomy_level ? ` · celda ${rule.autonomy_level}` : ""}` }),
    cellEvidence(rule) ? el("span", { class: "small", text: cellEvidence(rule) }) : null);
}

// The decision, as in the prototype: the three buttons, and the panel of a rejection or of a
// question opens on a click. The API checks the same as before; the form says what is missing.
function decisionCard(d, item, status) {
  const panel = decisionPanel(item);
  const head = el("div", { class: "stack tight-stack" },
    el("h2", { text: "Acción recomendada" }),
    el("p", { class: "soft-body", text: recommendationText(d) }));
  if (!panel.open) {
    return el("section", { class: "card soft" }, head,
      el("p", { class: "inner muted", text: `El caso no está en la cola. Estado: ${statusLabel(status)}.` }));
  }
  const message = el("p", { class: "small error", role: "status", hidden: true });
  const done = el("div", { class: "inner check", hidden: true });
  const note = el("textarea", { id: "decision-note", maxlength: "1000", placeholder: "Nota interna (opcional). Se redacta antes de guardarse." });
  const question = el("textarea", { id: "info-question", maxlength: "1000", placeholder: "Pregunta para el cliente. La verá tal como la escribas." });
  let reason = "";

  async function send(decision, button) {
    const fields = { reason, question: question.value, note: note.value };
    const problem = decisionProblem(decision, fields);
    if (problem) {
      message.textContent = problem;
      message.hidden = false;
      return;
    }
    message.hidden = true;
    button.disabled = true;
    try {
      const body = { decision, note: fields.note.trim() || undefined };
      if (decision === "reject") body.reason = fields.reason;
      if (decision === "need_info") body.question = fields.question.trim();
      const out = await api.call(`/cases/${encodeURIComponent(d.case_id)}/decision`, { method: "POST", body });
      done.replaceChildren(el("div", {},
        el("span", { class: "title", text: decisionDone(out) }),
        el("span", { class: "src", text: `audit_log · trace_id ${d.trace_id}` })));
      done.hidden = false;
      setTimeout(() => renderCase(d.case_id), 1200);
    } catch (e) {
      message.textContent = errorText(e);
      message.hidden = false;
      button.disabled = false;
    }
  }

  const rejectPanel = el("div", { class: "inner stack", hidden: true });
  const askPanel = el("div", { class: "inner stack", hidden: true });
  const toggle = (open) => {
    rejectPanel.hidden = open !== rejectPanel || !rejectPanel.hidden;
    askPanel.hidden = open !== askPanel || !askPanel.hidden;
    message.hidden = true;
  };

  const approve = el("button", { type: "button", class: "btn primary", disabled: !panel.canApprove, text: panel.approveLabel });
  approve.addEventListener("click", () => send("approve", approve));
  const reject = el("button", { type: "button", class: "btn", text: panel.rejectLabel, onclick: () => toggle(rejectPanel) });
  const ask = panel.canAsk
    ? el("button", { type: "button", class: "btn", text: "Pedir información", onclick: () => toggle(askPanel) })
    : null;

  const confirmReject = el("button", { type: "button", class: "btn primary medium", disabled: true, text: panel.confirmRejectLabel });
  confirmReject.addEventListener("click", () => send("reject", confirmReject));
  const reasons = REVERSAL_REASONS.map(([code, text]) => el("button", {
    type: "button", class: "pick", "aria-pressed": "false", text,
    onclick: (event) => {
      reason = code;
      for (const b of reasons) b.setAttribute("aria-pressed", String(b === event.currentTarget));
      confirmReject.disabled = false;
    },
  }));
  rejectPanel.append(
    el("span", { class: "small muted strong", text: panel.reasonTitle }),
    el("div", { class: "pills" }, reasons),
    el("div", {}, confirmReject));

  const sendQuestion = el("button", { type: "button", class: "btn primary medium", text: "Enviar pregunta" });
  sendQuestion.addEventListener("click", () => send("need_info", sendQuestion));
  askPanel.append(
    el("label", { for: "info-question", text: "Pregunta al cliente" }),
    question,
    el("p", { class: "small muted", text: "El cliente la ve en Mis aclaraciones y responde dentro del plazo en días hábiles de la política; al enviarla verás la fecha. Se muestra en español aunque el cliente escriba en portugués." }),
    el("div", {}, sendQuestion));

  return el("section", { class: "card soft decision" }, head,
    el("p", { class: "small muted", text: panel.approveHint }),
    el("div", { class: "row decision-buttons" }, approve, reject, ask),
    rejectPanel,
    askPanel,
    el("div", {}, el("label", { for: "decision-note", text: "Nota" }), note),
    message,
    done);
}

window.addEventListener("hashchange", route);
route();

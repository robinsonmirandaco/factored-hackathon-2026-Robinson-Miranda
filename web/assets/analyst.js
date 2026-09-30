// Analyst console (TRZ-27, TRZ-28): login, the queue with its filters, the dossier of a case and
// the three decisions. Every figure is read from the API; text reaches the page only through
// textContent, never as HTML.

import { createClient } from "./api.js";
import {
  REVERSAL_REASONS, actionLabel, actionStateLabel, candidateCards, clueChips, decisionDone,
  decisionPanel, decisionProblem, factRows, filterChips, identificationLabel, kindLabel, queueRow,
  reasonLabel, statusLabel,
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
  decision_not_allowed: "Un evento de seguridad solo se puede cerrar.",
  reason_required: "Elige un motivo de la lista.",
  reason_not_allowed: "El motivo no está en la lista.",
  question_required: "Escribe la pregunta para el cliente.",
  db_unavailable: "La base de datos no responde. Intenta de nuevo.",
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
  $("nav").hidden = !loggedIn;
  $("who").hidden = !loggedIn;
  $("logout").hidden = !loggedIn;
}

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
  for (const name of ["login", "queue", "case"]) $(`view-${name}`).hidden = name !== view;
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
  show("queue");
  return renderQueue();
}

async function renderQueue() {
  const target = $("view-queue");
  try {
    const params = filter === "all" ? "" : `?filter=${encodeURIComponent(filter)}`;
    const queue = await api.call(`/queue${params}`);
    const now = Date.now();
    const chips = filterChips(queue.counts, filter).map((c) => el("button", {
      type: "button",
      class: "chip filter",
      "aria-pressed": String(c.pressed),
      onclick: () => { filter = c.key; renderQueue(); },
    }, el("span", { text: c.label }), el("span", { class: "count", text: String(c.count) })));
    const rows = queue.items.map((item) => {
      const r = queueRow(item, now);
      return el("li", {}, el("a", { class: "list-row", href: r.href },
        el("span", { class: "initial", text: r.initial }),
        el("span", { class: "main" },
          el("div", { class: "title", text: r.title }),
          el("div", { class: "sub mono", text: r.sub })),
        el("span", { class: "end" },
          r.amount ? el("strong", { text: r.amount }) : null,
          el("span", { class: "row" }, r.pills.map(pill)))));
    });
    target.replaceChildren(
      el("p", { class: "eyebrow", text: "Operación" }),
      el("h1", { text: "Cola de casos" }),
      el("div", { class: "chips queue-filters" }, chips),
      el("div", { class: "card" },
        rows.length
          ? el("ul", { class: "list" }, rows)
          : el("p", { class: "muted", text: "No hay casos en este filtro." })),
      el("p", { class: "muted small", text: "El SLA corre en el reloj real, según la prioridad. Los montos en USD son los que comparó la política." }));
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
    target.replaceChildren(
      el("a", { class: "back", href: "#/cola", text: "← Cola de casos" }),
      el("p", { class: "eyebrow", text: `${kindLabel(dossier.case_kind)} · ${dossier.case_id}` }),
      el("h1", { text: dossier.request_summary }),
      el("div", { class: "case-grid" },
        el("div", { class: "case-main" }, dossierCards(dossier, history)),
        el("aside", { class: "case-side" }, decisionCard(dossier, item, one.status), actionsCard(dossier))));
  } catch (e) {
    failure(target, e);
  }
}

function dossierCards(d, history) {
  const cards = [];
  if (d.original_message) {
    cards.push(el("div", { class: "card" },
      el("h2", { text: "Mensaje del cliente" }),
      el("p", { class: "quote", text: d.original_message }),
      d.machine_translation
        ? el("div", { class: "stack" },
          el("p", { class: "small muted" }, "Traducción ", el("span", { class: "sim", text: "automática" }),
            d.machine_translation.model ? ` · ${d.machine_translation.model}` : ""),
          el("p", { class: "quote", text: d.machine_translation.text || "La traducción no está disponible." }))
        : null,
      el("p", { class: "small muted", text: `Idioma: ${(d.language || "?").toUpperCase()} · trace_id ${d.trace_id}` })));
  }
  const chips = clueChips(d.extraction);
  if (chips.length) {
    cards.push(el("div", { class: "card" },
      el("h2", { text: "Lo que se entendió" }),
      el("div", { class: "chips" }, chips.map((c) => el("span", { class: "chip", title: c.evidence },
        el("span", { class: "k", text: c.label }), el("span", { class: "v", text: c.value })))),
      el("p", { class: "small muted", text: "Cada pista trae su fragmento literal del mensaje (pasa el cursor)." })));
  }
  const facts = factRows(d.verified_facts);
  if (facts.length) cards.push(factCard("Hechos verificados", facts));
  const candidates = candidateCards(d.identification);
  if (candidates.length) {
    cards.push(el("div", { class: "card" },
      el("div", { class: "card-head" },
        el("h2", { text: "Identificación" }),
        el("span", { class: "muted small", text: `${d.identification.candidates} candidatos · ${identificationLabel(d.identification.decision)}` })),
      el("div", { class: "candidates" }, candidates.map((c) => el("div", { class: `candidate${c.inSet ? " in-set" : ""}` },
        el("div", { class: "row" }, el("span", { class: "mono", text: c.id }), el("span", { class: "spacer" }),
          c.inSet ? el("span", { class: "pill info", text: "En el conjunto" }) : null),
        el("div", { class: "probability", text: c.probability }),
        el("div", { class: "small muted mono", text: c.parts }))))));
  }
  const evidence = factRows(d.evidence);
  if (evidence.length) cards.push(factCard("Evidencia", evidence));
  if (d.later_messages.length) {
    cards.push(el("div", { class: "card" },
      el("h2", { text: "Mensajes después del paso a una persona" }),
      d.later_messages.map((m) => el("div", { class: "stack" },
        el("p", { class: "quote", text: m.text }),
        el("p", { class: "small muted mono", text: `${m.source.table} · ${m.source.id}` })))));
  }
  if (d.open_questions.length) {
    cards.push(el("div", { class: "card" },
      el("h2", { text: "Preguntas abiertas" }),
      el("div", { class: "stack" }, d.open_questions.map((q) => el("p", { class: "notice", text: q.text })))));
  }
  cards.push(el("div", { class: "card" },
    el("h2", { text: "Historial" }),
    el("ol", { class: "timeline" }, history.map((h) => el("li", {},
      el("div", { text: h.text }),
      el("div", { class: "when mono", text: `${h.actor} · ${h.action}` }))))));
  return cards;
}

function factCard(title, rows) {
  return el("div", { class: "card" },
    el("h2", { text: title }),
    el("div", { class: "detail" }, rows.map((r) => el("div", {},
      el("div", { class: "k", text: r.label }),
      el("div", { class: "v", text: r.value }),
      el("div", { class: "src", text: r.source })))));
}

function actionsCard(d) {
  if (!d.actions_taken.length) return null;
  return el("div", { class: "card" },
    el("h2", { text: "Acciones del caso" }),
    el("ul", { class: "list" }, d.actions_taken.map((a) => el("li", { class: "row action-row" },
      el("span", { text: actionLabel(a.action) }), el("span", { class: "spacer" }),
      el("span", {
        class: `pill ${a.state === "verified" ? "ok" : a.state === "failed" ? "bad" : "warn"}`,
        text: actionStateLabel(a.state),
      })))));
}

function decisionCard(d, item, status) {
  const rule = d.policy_rule_triggered;
  const panel = decisionPanel(item);
  const head = [
    el("h2", { text: "Decisión" }),
    rule
      ? el("div", { class: "stack small" },
        el("div", {}, el("span", { class: "muted", text: "Regla: " }), reasonLabel(rule.rule)),
        el("div", { class: "mono muted", text: `${rule.rule} · política ${rule.version}` }),
        el("div", {}, el("span", { class: "muted", text: "Nivel: " }), `${rule.level}${rule.autonomy_level ? ` · celda ${rule.autonomy_level}` : ""}`))
      : null,
    d.recommended_action
      ? el("p", { class: "notice small", text: `Sugerencia: ${actionLabel(d.recommended_action)}` })
      : null,
  ];
  if (!panel.open) {
    return el("div", { class: "card decision" }, head,
      el("p", { class: "muted", text: `El caso no está en la cola. Estado: ${statusLabel(status)}.` }));
  }
  const message = el("p", { class: "small", role: "status" });
  const reason = el("select", { id: "reject-reason" },
    el("option", { value: "", text: "Elige un motivo" }),
    REVERSAL_REASONS.map(([code, text]) => el("option", { value: code, text })));
  const note = el("textarea", { id: "decision-note", maxlength: "1000", placeholder: "Nota interna (opcional). Se redacta antes de guardarse." });
  const question = el("textarea", { id: "info-question", maxlength: "1000", placeholder: "Pregunta para el cliente. La verá tal como la escribas." });

  async function send(decision, button) {
    const fields = { reason: reason.value, question: question.value, note: note.value };
    const problem = decisionProblem(decision, fields);
    message.className = "small error";
    if (problem) {
      message.textContent = problem;
      return;
    }
    button.disabled = true;
    try {
      const body = { decision, note: fields.note.trim() || undefined };
      if (decision === "reject") body.reason = fields.reason;
      if (decision === "need_info") body.question = fields.question.trim();
      const out = await api.call(`/cases/${encodeURIComponent(d.case_id)}/decision`, { method: "POST", body });
      message.className = "small";
      message.textContent = decisionDone(out);
      setTimeout(() => renderCase(d.case_id), 1200);
    } catch (e) {
      message.textContent = errorText(e);
      button.disabled = false;
    }
  }

  const approve = el("button", { type: "button", class: "btn primary block", disabled: !panel.canApprove, text: panel.approveLabel });
  approve.addEventListener("click", () => send("approve", approve));
  const reject = el("button", { type: "button", class: "btn block", text: "Rechazar" });
  reject.addEventListener("click", () => send("reject", reject));
  const ask = el("button", { type: "button", class: "btn block", text: "Pedir información" });
  ask.addEventListener("click", () => send("need_info", ask));
  return el("div", { class: "card decision" }, head,
    el("div", { class: "stack" },
      approve,
      el("p", { class: "small muted", text: panel.approveHint }),
      el("hr", { class: "divider" }),
      el("label", { for: "reject-reason", text: "Motivo del rechazo" }), reason, reject,
      panel.canAsk ? el("hr", { class: "divider" }) : null,
      panel.canAsk ? el("label", { for: "info-question", text: "Pregunta al cliente" }) : null,
      panel.canAsk ? question : null,
      panel.canAsk ? el("p", { class: "small muted", text: "El cliente la ve en Mis aclaraciones y responde dentro del plazo en días hábiles de la política; al enviarla verás la fecha. Se muestra en español aunque el cliente escriba en portugués." }) : null,
      panel.canAsk ? ask : null,
      el("hr", { class: "divider" }),
      el("label", { for: "decision-note", text: "Nota" }), note,
      message));
}

window.addEventListener("hashchange", route);
route();

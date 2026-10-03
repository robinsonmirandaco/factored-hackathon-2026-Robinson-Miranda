// What the analyst console says, built from API data (TRZ-27). Pure functions, no DOM: the
// console renders their output, and tests/web checks it. The console is in Spanish; a
// Portuguese message is shown with its automatic translation. Values of the records use the
// words and formats of the customer web (i18n.js), in Spanish.

import { day, label, money, translator } from "./i18n.js";
import { chipParts } from "./view.js";

const es = translator("es");

export const FILTERS = [
  ["all", "Todos"],
  ["high_priority", "Alta prioridad"],
  ["over_1000_usd", "Más de 1000 USD"],
  ["no_match", "Sin coincidencia"],
  ["verification_failed", "Verificación fallida"],
  ["audit", "Auditoría"],
];

const KIND = {
  escalation: "Escalado",
  audit_sample: "Auditoría",
  security_event: "Evento de seguridad",
};

const INTENT = {
  unrecognized_charge: "Cargo no reconocido",
  billing_error_amount: "Monto distinto",
  billing_error_duplicate: "Cobro duplicado",
  claim_status: "Estado de un reclamo",
  out_of_scope: "Fuera de alcance",
};

const REASON = {
  "escalate.automation_disabled": "Automatización desactivada",
  "escalate.comprehension_unavailable": "El LLM no respondió y las reglas no entendieron",
  "escalate.clarifications_exhausted": "Dos aclaraciones sin identificar el cargo",
  "escalate.amount_above_human_review": "Monto mayor a 1000 USD",
  "escalate.amount_unknown": "Monto sin conversión a USD",
  "escalate.open_dispute_last_90d": "Aclaración abierta en los últimos 90 días",
  "escalate.conformal_set_empty": "Ningún cargo coincide",
  "escalate.verification_failed": "Verificación fallida",
  "escalate.autonomy_a2": "La celda está en revisión humana (A2)",
  "approval.autonomy_a1": "La celda pide aprobación (A1)",
  "approval.amount_above_auto_register": "Monto entre 500 y 1000 USD",
  "security.security_event": "Intento de ver datos de otro cliente",
  "verification.registration_failed": "La relectura del registro no coincidió",
};

const PRIORITY = { urgent: "Urgente", high: "Alta", normal: "Normal" };

const STATUS = {
  escalated: "Escalado",
  pending_analyst_approval: "Espera aprobación",
  security_blocked: "Detenido por seguridad",
  failed: "Verificación fallida",
  awaiting_customer: "Esperando al cliente",
  approved: "Aprobado",
  rejected: "Rechazado",
  registered_verified: "Registrado y verificado",
  in_review: "En revisión por una analista",
  closed_no_info: "Cerrado por falta de información",
};

const ACTION = {
  register: "Registrar la aclaración",
  register_and_offer_block: "Registrar y ofrecer el bloqueo de la tarjeta",
  register_and_block: "Registrar y bloquear la tarjeta",
  block: "Bloquear la tarjeta",
};

const ACTION_STATE = {
  verified: "Verificada",
  not_executed: "No ejecutada",
  failed: "Fallida",
  not_verified: "Sin verificación",
};

// The closed list of the policy (autonomy.reversal_reasons), with the words of the history.
export const REVERSAL_REASONS = [
  ["wrong_charge", "Cargo equivocado"],
  ["should_not_act", "No debía actuar"],
  ["should_have_escalated", "Debía escalar"],
  ["misunderstanding_unresolved", "Malentendido sin resolver"],
  ["insufficient_data", "Datos insuficientes"],
  ["other", "Otro"],
];

const FACT = {
  customer_segment: "Segmento",
  customer_country: "País",
  amount: "Monto registrado",
  currency: "Moneda",
  merchant: "Comercio",
  date: "Fecha",
  channel: "Canal",
  status: "Estado de la transacción",
  amount_usd: "Monto en USD",
  dispute_status: "Estado de la aclaración",
  dispute_due_date: "Plazo de respuesta",
  identical_charge: "Cargo idéntico",
  product_status: "Estado de la tarjeta",
  open_dispute: "Aclaración abierta",
};

const CLUE = {
  amount: "Monto",
  date: "Fecha",
  merchant_hint: "Comercio",
  channel_hint: "Canal",
  card_in_possession: "Tarjeta",
};

const IDENTIFICATION = {
  identified: "un solo cargo posible",
  show_options: "opciones para el cliente",
  ask_for_detail: "pedir un dato",
  not_found: "ningún cargo encaja",
};

const words = (table, code) => (code == null ? "" : table[code] || code);

export const kindLabel = (code) => words(KIND, code);
export const intentLabel = (code) => words(INTENT, code);
// rho is read from the policy through the API, never written here (TRZ-29 CA3).
export function auditLabel(rho) {
  return `Muestra de auditoría (ρ = ${Number(rho).toFixed(2).replace(".", ",")})`;
}
export const reasonLabel = (code, rho) => (code === "audit.sample" && rho != null ? auditLabel(rho) : words(REASON, code));
export const statusLabel = (code) => words(STATUS, code);
export const actionLabel = (code) => words(ACTION, code);
export const actionStateLabel = (code) => words(ACTION_STATE, code);
export const factLabel = (code) => words(FACT, code);
export const clueLabel = (code) => words(CLUE, code);
export const identificationLabel = (code) => words(IDENTIFICATION, code);

export function usd(amount) {
  return amount == null ? "" : money("es", Number(amount), "USD");
}

const SEGMENT = { Basic: "Básico", Plus: "Plus", Premium: "Premium", Student: "Estudiante" };
const COUNTRY = { MX: "México", CO: "Colombia", AR: "Argentina" };
const DISPUTE = { opened: "Abierta", open: "Abierta", verification_failed: "Verificación fallida" };

export function percent(p) {
  return `${Math.round(Number(p) * 100)} %`;
}

// The API writes the real clock in UTC without an offset.
function utcMillis(iso) {
  return Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
}

function span(minutes) {
  const m = Math.max(0, Math.round(minutes));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

// The SLA is operation time, on the real clock: it is told as time left, never as a date, so
// the screens keep showing only dates of the simulated clock (design 10.2, rule 7).
export function slaText(dueIso, nowMs) {
  if (!dueIso) return { text: "Sin SLA", tone: "" };
  const left = (utcMillis(dueIso) - nowMs) / 60000;
  if (left < 0) return { text: `SLA vencido hace ${span(-left)}`, tone: "bad" };
  return { text: `SLA en ${span(left)}`, tone: left < 60 ? "warn" : "" };
}

export function filterChips(counts, active) {
  return FILTERS.map(([key, label]) => ({
    key,
    label,
    count: counts?.[key] ?? 0,
    pressed: (active || "all") === key,
  }));
}

// The security rule is the same for every stop; an injection says what it was.
export const INJECTION_REASON = "Instrucción inyectada en el mensaje";

// One row of the queue, a value per column. A security event shows no customer data (TRZ-27
// CA8): the API sends none, and the row says so instead of leaving blanks. CA8 protects another
// customer's data; an injection stop holds the customer's own, so its row is shown in full.
export function queueRow(item, nowMs, rho) {
  const security = item.kind === "security_event" && !item.injection;
  const tags = [];
  if (item.injection) tags.push({ text: "Inyección", tone: "bad" });
  if (item.kind === "audit_sample") tags.push({ text: "Auditoría", tone: "info" });
  if (item.status === "awaiting_customer") tags.push({ text: "Esperando al cliente", tone: "warn" });
  if (item.updated) tags.push({ text: "Actualizado", tone: "new" });
  // Created by the demo state, not by a customer (TRZ-38, design 10.2 rule 5).
  if (item.simulated) tags.push({ text: "[simulado]", tone: "sim" });
  return {
    href: `#/caso/${encodeURIComponent(item.case_id)}`,
    caseId: item.case_id,
    tags,
    customer: security ? "Sin datos del cliente" : `Cliente ${item.customer_id}`,
    kind: kindLabel(item.kind),
    type: security ? kindLabel(item.kind) : intentLabel(item.intent),
    amount: security ? null : item.amount_usd == null ? "Sin monto en USD" : usd(item.amount_usd),
    language: item.language ? item.language.toUpperCase() : "",
    reason: item.injection ? INJECTION_REASON : reasonLabel(item.reason, rho),
    priority: { text: PRIORITY[item.priority] || item.priority, tone: item.priority === "normal" ? "" : "bad" },
    sla: slaText(item.sla_due_at, nowMs),
  };
}

// What the decision panel offers for a case: only an open queue row can be decided.
export function decisionPanel(item) {
  if (!item) return { open: false };
  if (item.kind === "audit_sample") {
    // An audit sample is confirmed or reversed; nothing is asked of the customer (TRZ-29 CA5).
    return {
      open: true,
      audit: true,
      canApprove: true,
      approveLabel: "Confirmar",
      rejectLabel: "Revertir",
      reasonTitle: "Motivo de la reversión",
      confirmRejectLabel: "Confirmar reversión",
      approveHint: "Confirmar no cambia nada para el cliente. Revertir pasa su aclaración a revisión por una analista y le avisa; la disputa registrada no se anula.",
      canAsk: false,
    };
  }
  // An injection stop is decided like any other case (TRZ-46 follow-up).
  const security = item.kind === "security_event" && !item.injection;
  return {
    open: true,
    audit: false,
    rejectLabel: "Rechazar",
    reasonTitle: "Motivo del rechazo",
    confirmRejectLabel: "Confirmar rechazo",
    canApprove: Boolean(item.can_approve),
    approveLabel: security ? "Cerrar el evento" : "Aprobar",
    approveHint: item.can_approve
      ? security
        ? "Confirma la detención; no se ejecuta ninguna acción."
        : "Registra la aclaración y la verifica. El bloqueo de tarjeta no se ejecuta: requiere la confirmación del cliente."
      : "No hay acción que ejecutar: pide información o rechaza.",
    canAsk: !security,
  };
}

// The message split at the marked spans, so the injected instruction can be highlighted.
export function markedParts(text, spans) {
  const parts = [];
  let at = 0;
  for (const [start, end] of spans || []) {
    if (start > at) parts.push({ text: text.slice(at, start), marked: false });
    parts.push({ text: text.slice(start, end), marked: true });
    at = end;
  }
  if (at < text.length || !parts.length) parts.push({ text: text.slice(at), marked: false });
  return parts;
}

// The same checks the API makes, so the form says what is missing before it sends.
export function decisionProblem(decision, fields) {
  if (decision === "reject" && !fields.reason) return "Elige un motivo de la lista.";
  if (decision === "need_info" && !String(fields.question || "").trim()) return "Escribe la pregunta para el cliente.";
  return null;
}

// What a decision did, in one sentence for the analyst.
export function decisionDone(out) {
  if (out.decision === "approve" && out.status === "registered_verified") return "Auditoría confirmada: nada cambia para el cliente.";
  if (out.decision === "reject" && out.status === "in_review") return "Revertido: la aclaración pasó a revisión y se avisó al cliente. La disputa registrada no se anula.";
  if (out.decision === "approve" && out.dispute_folio) {
    const block = out.block_not_executed ? " El bloqueo de tarjeta no se ejecutó." : "";
    return `Aprobado: se registró ${out.dispute_folio} y se verificó.${block}`;
  }
  if (out.decision === "approve" && out.status === "failed") return "La relectura no coincidió: el caso volvió a la cola.";
  if (out.decision === "approve") return "Evento cerrado.";
  if (out.decision === "reject") return "Rechazado.";
  return `Pregunta enviada. El cliente puede responder hasta el ${day("es", out.due_on)}.`;
}

function factValue(f, currency) {
  const v = f.value;
  if (f.name === "amount") return currency ? money("es", Number(v), currency) : String(v);
  if (f.name === "amount_usd") return usd(v);
  if (f.name === "date" || f.name === "dispute_due_date") return day("es", v);
  if (f.name === "channel") return label(es, "channel", v);
  if (f.name === "status" || f.name === "identical_charge") return label(es, "tx", v);
  if (f.name === "product_status") return label(es, "pstatus", v);
  if (f.name === "customer_segment") return words(SEGMENT, v);
  if (f.name === "customer_country") return words(COUNTRY, v);
  if (f.name === "dispute_status" || f.name === "open_dispute") return words(DISPUTE, v);
  return String(v);
}

// The currency is shown with its amount, not as a row of its own.
export function factRows(facts) {
  const list = facts || [];
  const currency = list.find((f) => f.name === "currency")?.value;
  return list.filter((f) => f.name !== "currency").map((f) => ({
    label: factLabel(f.name),
    value: f.value == null ? "Sin dato" : factValue(f, currency),
    source: `${f.source.table} · ${f.source.id}`,
  }));
}

// Each question of the analyst with the customer's answer, in the order they were asked.
export function infoExchanges(exchanges) {
  return (exchanges || []).map((e) => ({
    question: e.question,
    answer: e.answer ?? (e.status === "expired" ? "Sin respuesta: venció el plazo" : "Sin respuesta todavía"),
    answered: e.answer != null,
    meta: `${e.asked_by} · ${day("es", e.asked_on)} · plazo ${day("es", e.due_on)}`,
    source: `${e.source.table} · ${e.source.id}`,
  }));
}

export function clueChips(extraction) {
  return (extraction || []).map((c) => {
    const v = c.value && typeof c.value === "object" ? c.value : { value: c.value };
    let value = v.value;
    if (c.field === "amount") value = v.currency ? money("es", Number(v.value), v.currency) : String(v.value);
    if (c.field === "card_in_possession") value = v.value ? "La tiene" : "No la tiene";
    // The days the words mean, as the customer's chip gives them; the words are the evidence.
    if (c.field === "date") {
      value = v.window_from && v.window_to
        ? chipParts(translator("es"), "es", { ...v, field: "date", evidence: c.evidence }).value
        : v.expression || v.kind || c.evidence;
    }
    return { label: clueLabel(c.field), value: String(value ?? ""), evidence: c.evidence };
  });
}

// The recommended action of a dossier, or why there is none.
export function recommendationText(d) {
  if (d.recommendation_hidden) {
    return "Sin propuesta: la celda está en A2 (solo analista). Aprobar registra la aclaración sobre el cargo identificado.";
  }
  if (d.recommended_action) return actionLabel(d.recommended_action);
  if (d.case_kind !== "security_event" && d.charge_identified === false) {
    return "Sin acción recomendada: no se identificó ningún cargo.";
  }
  return "Sin acción recomendada.";
}

const comma = (x, digits) => Number(x).toFixed(digits).replace(".", ",");

// Why the cell of the case is at its level: r, W and N of the block that set it (TRZ-30).
export function cellEvidence(rule) {
  const c = rule?.autonomy_change;
  if (!c || !rule.autonomy_level) return null;
  let crossed = "";
  if (c.threshold === "demote_if_wilson_lower_gte") crossed = ` ≥ ${comma(c.threshold_value, 2)}`;
  if (c.threshold === "promote_if_rate_lt") crossed = `, bloques seguidos con r < ${comma(c.threshold_value, 2)}`;
  return `Celda en ${rule.autonomy_level} desde un bloque de ${c.n} revisiones con ${c.reversals} reversiones: ` +
    `r = ${comma(c.r, 2)}, W = ${comma(c.w, 3)}${crossed} (N = ${c.n}). Antes estaba en ${c.level_before}.`;
}

// Candidates with their probability: percentages are for the analyst only (rule 3).
export function candidateCards(identification) {
  if (!identification) return [];
  const chosen = new Set(identification.conformal_set);
  return identification.top.map((c) => ({
    id: c.transaction_id,
    probability: percent(c.probability),
    inSet: chosen.has(c.transaction_id),
    parts: Object.entries(c.components).map(([k, v]) => `${k} ${Number(v).toFixed(2)}`).join(" · "),
    scores: Object.entries(c.components).map(([k, v]) => [k, Number(v).toFixed(2)]),
  }));
}

// The identification table of a dossier as flat text: the header, then one string per cell of
// each candidate, with its id and whether it is in the conformal set.
export function identificationTable(identification) {
  const candidates = candidateCards(identification);
  const keys = candidates.length ? candidates[0].scores.map(([k]) => k) : [];
  return {
    header: ["Transacción", ...keys, "p"],
    rows: candidates.map((c) => ({
      id: c.id,
      inSet: c.inSet,
      cells: [...c.scores.map(([, v]) => v), c.probability],
    })),
  };
}

// The heading of a dossier: the kind of case while it is with a person, its status once an
// analyst decided it or asked the customer (regression of the queue QA).
export function caseHeading(kind, status, caseId) {
  const decided = ["approved", "rejected", "awaiting_customer", "in_review", "closed_no_info"].includes(status);
  const parts = kind === "security_event"
    ? [kindLabel(kind), decided ? statusLabel(status) : null]
    : [decided ? statusLabel(status) : kindLabel(kind)];
  return [...parts.filter(Boolean), caseId].join(" · ");
}

// The global automation switch (TRZ-35), as the header says it and as its dialog asks it. Both
// directions ask before they change.
export function automationView(state) {
  const off = Boolean(state?.all_to_human);
  const action = off ? "Reactivar la automatización" : "Mandar todo a humano";
  return {
    text: off ? "Todo a humano" : "Automatización activa",
    tone: off ? "bad" : "ok",
    action,
    title: `¿${action}?`,
    confirm: off
      ? "Los casos vuelven a registrarse solos según la política."
      : "Ninguna aclaración se registrará sola: todas irán a la cola con el motivo «Automatización desactivada».",
    confirmLabel: action,
    cancelLabel: "Cancelar",
  };
}

// The demo reset (TRZ-38), as its header button says it and as the dialog asks it.
export function demoResetView() {
  return {
    title: "¿Reiniciar el demo?",
    confirm: "Se borran los casos, la cola, las disputas, los bloqueos, las notificaciones y el "
      + "audit log del demo, las tarjetas bloqueadas vuelven a su estado y se crean de nuevo los "
      + "casos [simulado] de la cola. La celda PT-BR vuelve a 19 revisiones con 9 reversiones, "
      + "en A0, y las demás celdas de autonomía empiezan de cero. Las sesiones de los clientes "
      + "se cierran.",
    confirmLabel: "Reiniciar demo",
    cancelLabel: "Cancelar",
  };
}

// What a key does while the dialog is open: Escape cancels, Tab stays between its buttons.
export function dialogKey(key, shift, focusedIndex, count) {
  if (key === "Escape") return { cancel: true };
  if (key !== "Tab" || count === 0) return null;
  const next = shift ? (focusedIndex - 1 + count) % count : (focusedIndex + 1) % count;
  return { focus: focusedIndex < 0 ? (shift ? count - 1 : 0) : next };
}

// ---- Estado de autonomía (TRZ-31) ---------------------------------------------------------

// The levels of design 6.7, with their names.
export const LEVELS = {
  A0: "Autónomo con confirmación del cliente",
  A1: "Requiere aprobación de analista",
  A2: "Solo analista",
};

const COUNT_WORDS = { 1: "un", 2: "dos", 3: "tres", 4: "cuatro" };

// The thresholds of the policy, as read from the API, above the table (CA2).
export function thresholdsText(t) {
  const blocks = COUNT_WORDS[t.promote_after_consecutive_windows] || String(t.promote_after_consecutive_windows);
  return [
    `N = ${t.window_n}`,
    `Baja si W ≥ ${comma(t.demote_if_wilson_lower_gte, 2)}`,
    `Sube con ${blocks} bloques seguidos con r < ${comma(t.promote_if_rate_lt, 2)}`,
    `z = ${comma(t.z, 3)}`,
  ];
}

// The figures of a closed block, as the history and the dossier write them.
function blockFigures(b) {
  let crossed = "";
  if (b.threshold === "demote_if_wilson_lower_gte") crossed = ` ≥ ${comma(b.threshold_value, 2)}`;
  if (b.threshold === "promote_if_rate_lt") crossed = `, bloques seguidos con r < ${comma(b.threshold_value, 2)}`;
  return `${b.reversals} de ${b.n}, r = ${comma(b.r, 2)}, W = ${comma(b.w, 3)}${crossed}`;
}

// One row of the table (CA1). W exists only for a closed block: with an open block of fewer
// than N reviews nothing is computed (design 6.7). The last change has no date: the audit log
// keeps the real clock, and the screens show only simulated dates (design 10.2, rule 7).
export function autonomyRow(cell, thresholds) {
  const n = thresholds.window_n;
  const last = cell.last_block;
  const change = cell.last_change;
  return {
    cell: `${intentLabel(cell.intent)} · ${cell.language.toUpperCase()}`,
    level: cell.level,
    levelText: LEVELS[cell.level] || cell.level,
    reviews: `${cell.block_reviews} de ${n}`,
    reversals: String(cell.block_reversals),
    rate: cell.rate == null ? "Sin revisiones" : comma(cell.rate, 2),
    w: last ? comma(last.w, 3) : `Se calcula con N = ${n}`,
    wNote: last ? `Último bloque cerrado: ${blockFigures(last)}` : null,
    change: change
      ? `${change.level_before} → ${change.level_after} con ${blockFigures(change)}`
      : "Sin cambios de nivel",
    changeCase: change?.closed_by || null,
    reversedCount: cell.reversed_last_block.length + cell.reversed_open_block.length,
  };
}

const reasonWords = Object.fromEntries(REVERSAL_REASONS);

function reversedItem(item) {
  return {
    caseId: item.case_id,
    href: `#/caso/${encodeURIComponent(item.case_id)}`,
    reason: reasonWords[item.reason] || item.reason,
    simulated: item.simulated,
  };
}

// "Ver casos" (CA3): the reversed cases of the last closed block, which decided its level, and
// apart those of the open block.
export function reversedGroups(cell) {
  const groups = [];
  const last = cell.last_block;
  if (last) {
    const decided = last.changed
      ? `produjo el cambio de ${last.level_before} a ${last.level_after}`
      : `la celda siguió en ${last.level_after}`;
    groups.push({
      title: `Último bloque cerrado (${last.reversals} de ${last.n}, ${decided})`,
      items: cell.reversed_last_block.map(reversedItem),
    });
  }
  groups.push({
    title: `Bloque abierto (${cell.block_reversals} de ${cell.block_reviews} revisiones)`,
    items: cell.reversed_open_block.map(reversedItem),
  });
  return groups;
}

// Below the table when the reversals come from the demo or the harness (CA5).
export const SIMULATED_NOTE = "Reversiones simuladas contra verdad de terreno en la evaluación";

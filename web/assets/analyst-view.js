// What the analyst console says, built from API data (TRZ-27). Pure functions, no DOM: the
// console renders their output, and tests/web checks it. The console is in Spanish; a
// Portuguese message is shown with its automatic translation. Values of the records use the
// words and formats of the customer web (i18n.js), in Spanish.

import { day, label, money, translator } from "./i18n.js";

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
export const reasonLabel = (code) => words(REASON, code);
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

// One row of the queue. A security event shows no customer data (TRZ-27 CA8): the API sends
// none, and the row says so instead of leaving blanks.
export function queueRow(item, nowMs) {
  const security = item.kind === "security_event";
  const title = security
    ? kindLabel(item.kind)
    : [intentLabel(item.intent), reasonLabel(item.reason)].filter(Boolean).join(" · ");
  const sub = [
    item.case_id,
    security ? "Sin datos del cliente" : `Cliente ${item.customer_id}`,
    item.language ? item.language.toUpperCase() : null,
    security ? reasonLabel(item.reason) : kindLabel(item.kind),
  ].filter(Boolean).join(" · ");
  const sla = slaText(item.sla_due_at, nowMs);
  const pills = [
    { text: `Prioridad ${PRIORITY[item.priority] || item.priority}`, tone: item.priority === "normal" ? "" : "bad" },
    { text: sla.text, tone: sla.tone },
  ];
  if (item.updated) pills.push({ text: "Actualizado", tone: "info" });
  return {
    href: `#/caso/${encodeURIComponent(item.case_id)}`,
    initial: security ? "S" : item.kind === "audit_sample" ? "A" : "E",
    title,
    sub,
    amount: security ? null : item.amount_usd == null ? "Sin monto en USD" : usd(item.amount_usd),
    pills,
  };
}

// What the decision panel offers for a case: only an open queue row can be decided.
export function decisionPanel(item) {
  if (!item) return { open: false };
  const security = item.kind === "security_event";
  return {
    open: true,
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

// The same checks the API makes, so the form says what is missing before it sends.
export function decisionProblem(decision, fields) {
  if (decision === "reject" && !fields.reason) return "Elige un motivo de la lista.";
  if (decision === "need_info" && !String(fields.question || "").trim()) return "Escribe la pregunta para el cliente.";
  return null;
}

// What a decision did, in one sentence for the analyst.
export function decisionDone(out) {
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
    if (c.field === "date") value = v.expression || v.kind || c.evidence;
    return { label: clueLabel(c.field), value: String(value ?? ""), evidence: c.evidence };
  });
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
  }));
}

// The heading of a dossier: the kind of case while it is with a person, its status once an
// analyst decided it or asked the customer (regression of the queue QA).
export function caseHeading(kind, status, caseId) {
  const decided = ["approved", "rejected", "awaiting_customer"].includes(status);
  const parts = kind === "security_event"
    ? [kindLabel(kind), decided ? statusLabel(status) : null]
    : [decided ? statusLabel(status) : kindLabel(kind)];
  return [...parts.filter(Boolean), caseId].join(" · ");
}

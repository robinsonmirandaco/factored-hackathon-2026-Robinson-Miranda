// The analyst console (TRZ-27): queue rows, filter counters, SLA, the decision panel and form.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  REVERSAL_REASONS, decisionDone, decisionPanel, decisionProblem, filterChips, identificationTable,
  queueRow, slaText,
} from "../../web/assets/analyst-view.js";

const NOW = Date.parse("2026-09-30T12:00:00Z");
const item = {
  queue_id: 7, case_id: "CASE-1", kind: "escalation", customer_id: "C1",
  intent: "unrecognized_charge", amount_usd: 800, language: "es",
  reason: "approval.amount_above_auto_register", priority: "normal",
  sla_due_at: "2026-09-30T15:30:00", overdue: false, updated: false,
  status: "pending_analyst_approval", recommended_action: "register_and_offer_block",
  can_approve: true, created_at: "2026-09-30T11:30:00",
};

test("a queue row shows case, customer, language, reason, amount, priority and SLA", () => {
  const r = queueRow(item, NOW);
  assert.equal(r.href, "#/caso/CASE-1");
  assert.equal(r.caseId, "CASE-1");
  assert.equal(r.customer, "Cliente C1");
  assert.equal(r.kind, "Escalado");
  assert.equal(r.type, "Cargo no reconocido");
  assert.equal(r.reason, "Monto entre 500 y 1000 USD");
  assert.equal(r.language, "ES");
  assert.match(r.amount, /^USD\s800\.00$/);
  assert.deepEqual(r.priority, { text: "Normal", tone: "" });
  assert.equal(r.sla.text, "SLA en 3 h 30 min");
  assert.deepEqual(r.tags, []);
});

test("a case that came back with the customer's answer is marked updated", () => {
  const r = queueRow({ ...item, updated: true }, NOW);
  assert.deepEqual(r.tags.map((x) => x.text), ["Actualizado"]);
});

test("an audit sample and a case waiting for the customer are tagged", () => {
  const r = queueRow({ ...item, kind: "audit_sample", status: "awaiting_customer" }, NOW);
  assert.deepEqual(r.tags.map((x) => x.text), ["Auditoría", "Esperando al cliente"]);
});

test("a security event row says it has no customer data", () => {
  const security = {
    ...item, kind: "security_event", customer_id: null, intent: null, amount_usd: null,
    reason: "security.security_event", priority: "urgent",
  };
  const r = queueRow(security, NOW);
  assert.equal(r.type, "Evento de seguridad");
  assert.equal(r.customer, "Sin datos del cliente");
  assert.equal(r.reason, "Intento de ver datos de otro cliente");
  assert.equal(r.amount, null);
  assert.deepEqual(r.priority, { text: "Urgente", tone: "bad" });
});

test("the SLA is told as time left, or as overdue, never as a date", () => {
  assert.deepEqual(slaText("2026-09-30T12:40:00", NOW), { text: "SLA en 40 min", tone: "warn" });
  assert.deepEqual(slaText("2026-09-30T10:00:00", NOW), { text: "SLA vencido hace 2 h", tone: "bad" });
  assert.deepEqual(slaText(null, NOW), { text: "Sin SLA", tone: "" });
});

test("every filter chip carries the counter the API gave", () => {
  const counts = { all: 4, high_priority: 1, over_1000_usd: 1, no_match: 1, verification_failed: 0, audit: 0 };
  const chips = filterChips(counts, "no_match");
  assert.deepEqual(chips.map((c) => [c.key, c.count]), Object.entries(counts));
  assert.deepEqual(chips.filter((c) => c.pressed).map((c) => c.key), ["no_match"]);
});

test("approve is disabled when there is nothing to run", () => {
  assert.equal(decisionPanel({ ...item, can_approve: false }).canApprove, false);
  assert.match(decisionPanel({ ...item, can_approve: false }).approveHint, /pide información o rechaza/);
  assert.match(decisionPanel(item).approveHint, /bloqueo de tarjeta no se ejecuta/);
  assert.equal(decisionPanel(null).open, false);
});

test("a security event can only be closed", () => {
  const panel = decisionPanel({ ...item, kind: "security_event", can_approve: true });
  assert.equal(panel.approveLabel, "Cerrar el evento");
  assert.equal(panel.canAsk, false);
});

test("the form asks for a reason to reject and a question to ask", () => {
  assert.equal(decisionProblem("reject", { reason: "" }), "Elige un motivo de la lista.");
  assert.equal(decisionProblem("reject", { reason: "other" }), null);
  assert.equal(decisionProblem("need_info", { question: "  " }), "Escribe la pregunta para el cliente.");
  assert.equal(decisionProblem("approve", {}), null);
});

test("the reasons are the closed list of the policy", () => {
  assert.deepEqual(REVERSAL_REASONS.map(([code]) => code), [
    "wrong_charge", "should_not_act", "should_have_escalated", "misunderstanding_unresolved",
    "insufficient_data", "other",
  ]);
});

test("what a decision did, in one sentence", () => {
  assert.equal(
    decisionDone({ decision: "approve", status: "approved", dispute_folio: "DSP-2026-00001", block_not_executed: true }),
    "Aprobado: se registró DSP-2026-00001 y se verificó. El bloqueo de tarjeta no se ejecutó.",
  );
});

// QA of the queue (2026-09-30), findings 1, 3 and 4.
import { clueChips, factRows, infoExchanges } from "../../web/assets/analyst-view.js";

const src = (table, id) => ({ table, id });

test("facts use the words and formats of the customer web, in Spanish", () => {
  const rows = factRows([
    { name: "customer_segment", value: "Student", source: src("customers", "C1") },
    { name: "customer_country", value: "CO", source: src("customers", "C1") },
    { name: "amount", value: 257.21, source: src("transactions", "T1") },
    { name: "currency", value: "COP", source: src("transactions", "T1") },
    { name: "date", value: "2026-06-15", source: src("transactions", "T1") },
    { name: "channel", value: "POS", source: src("transactions", "T1") },
    { name: "status", value: "Approved", source: src("transactions", "T1") },
    { name: "amount_usd", value: 800, source: src("transactions", "T1") },
    { name: "dispute_status", value: "opened", source: src("disputes", "DSP-2026-00001") },
    { name: "dispute_due_date", value: "2026-07-08", source: src("disputes", "DSP-2026-00001") },
    { name: "product_status", value: "Active", source: src("products", "P1") },
  ]);
  const shown = Object.fromEntries(rows.map((r) => [r.label, r.value]));
  assert.equal(shown["Segmento"], "Estudiante");
  assert.equal(shown["País"], "Colombia");
  assert.match(shown["Monto registrado"], /^COP\s257\.21$/);
  assert.equal(shown["Moneda"], undefined);
  assert.equal(shown["Fecha"], "15 jun 2026");
  assert.equal(shown["Canal"], "Terminal en comercio");
  assert.equal(shown["Estado de la transacción"], "Aprobada");
  assert.match(shown["Monto en USD"], /^USD\s800\.00$/);
  assert.equal(shown["Estado de la aclaración"], "Abierta");
  assert.equal(shown["Plazo de respuesta"], "8 jul 2026");
  assert.equal(shown["Estado de la tarjeta"], "Activa");
});

test("an amount clue is shown like the customer web", () => {
  const [chip] = clueChips([{ field: "amount", value: { value: 800, currency: "USD", approximate: false }, evidence: "800 dólares", read_in: 1 }]);
  assert.match(chip.value, /^USD\s800\.00$/);
});

test("the deadline of a request for information is a day, not an ISO date", () => {
  assert.equal(decisionDone({ decision: "need_info", status: "awaiting_customer", due_on: "2026-06-24" }),
    "Pregunta enviada. El cliente puede responder hasta el 24 jun 2026.");
});

test("each question of the analyst is shown with its answer, in order", () => {
  const rows = infoExchanges([
    { question: "¿Primera?", asked_by: "analista.demo", asked_on: "2026-06-17", due_on: "2026-06-24", status: "answered", answer: "Sí", source: src("info_requests", "1") },
    { question: "¿Segunda?", asked_by: "analista.demo", asked_on: "2026-06-17", due_on: "2026-06-24", status: "open", answer: null, source: src("info_requests", "2") },
  ]);
  assert.deepEqual(rows.map((r) => [r.question, r.answer]), [["¿Primera?", "Sí"], ["¿Segunda?", "Sin respuesta todavía"]]);
  assert.equal(rows[0].meta, "analista.demo · 17 jun 2026 · plazo 24 jun 2026");
});

// Regression of the queue QA (2026-09-30), finding 2.
import { caseHeading } from "../../web/assets/analyst-view.js";

test("the heading of a decided case shows its final status, not the kind", () => {
  assert.equal(caseHeading("escalation", "rejected", "CASE-1"), "Rechazado · CASE-1");
  assert.equal(caseHeading("escalation", "approved", "CASE-1"), "Aprobado · CASE-1");
  assert.equal(caseHeading("escalation", "awaiting_customer", "CASE-1"), "Esperando al cliente · CASE-1");
  assert.equal(caseHeading("escalation", "escalated", "CASE-1"), "Escalado · CASE-1");
  assert.equal(caseHeading("security_event", "approved", "CASE-1"), "Evento de seguridad · Aprobado · CASE-1");
});

// Seen in the demo rehearsal: the dossier table showed "[object HTMLSpanElement]" because its
// cells came as nested arrays. The table is now flat text, one string per cell.
test("the identification table is flat text: a header and one string per cell", () => {
  const identification = {
    decision: "show_options",
    candidates: 2,
    conformal_set: ["T1"],
    top: [
      { transaction_id: "T1", probability: 0.6, components: { date: -4, amount: 0, merchant: 0.3 } },
      { transaction_id: "T2", probability: 0.4, components: { date: 0, amount: -4, merchant: 0.1 } },
    ],
  };
  const table = identificationTable(identification);
  assert.deepEqual(table.header, ["Transacción", "date", "amount", "merchant", "p"]);
  assert.deepEqual(table.rows.map((r) => [r.id, r.inSet]), [["T1", true], ["T2", false]]);
  assert.deepEqual(table.rows[0].cells, ["-4.00", "0.00", "0.30", "60 %"]);
  for (const row of table.rows) assert.ok(row.cells.every((c) => typeof c === "string"));
});

test("an identification with no candidate has no table rows", () => {
  const table = identificationTable({ decision: "not_found", candidates: 0, conformal_set: [], top: [] });
  assert.deepEqual(table.rows, []);
});

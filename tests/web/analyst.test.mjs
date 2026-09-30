// The analyst console (TRZ-27): queue rows, filter counters, SLA, the decision panel and form.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  REVERSAL_REASONS, decisionDone, decisionPanel, decisionProblem, filterChips, queueRow, slaText,
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
  assert.equal(r.title, "Cargo no reconocido · Monto entre 500 y 1000 USD");
  assert.equal(r.sub, "CASE-1 · Cliente C1 · ES · Escalado");
  assert.equal(r.amount, "US$800.00");
  assert.deepEqual(r.pills.map((p) => p.text), ["Prioridad Normal", "SLA en 3 h 30 min"]);
});

test("a case that came back with the customer's answer is marked updated", () => {
  const r = queueRow({ ...item, updated: true }, NOW);
  assert.equal(r.pills.at(-1).text, "Actualizado");
});

test("a security event row says it has no customer data", () => {
  const security = {
    ...item, kind: "security_event", customer_id: null, intent: null, amount_usd: null,
    reason: "security.security_event", priority: "urgent",
  };
  const r = queueRow(security, NOW);
  assert.equal(r.title, "Evento de seguridad");
  assert.equal(r.sub, "CASE-1 · Sin datos del cliente · ES · Intento de ver datos de otro cliente");
  assert.equal(r.amount, null);
  assert.equal(r.initial, "S");
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
  assert.equal(decisionDone({ decision: "need_info", status: "awaiting_customer", due_on: "2026-06-24" }),
    "Pregunta enviada. El cliente puede responder hasta el 2026-06-24.");
});

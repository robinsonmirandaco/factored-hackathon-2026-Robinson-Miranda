// The demo state in the analyst console (TRZ-38): seeded cases are labeled and the reset asks first.
import assert from "node:assert/strict";
import { test } from "node:test";

import { demoResetView, queueRow } from "../../web/assets/analyst-view.js";

const NOW = Date.parse("2026-09-30T12:00:00Z");
const item = {
  queue_id: 7, case_id: "CASE-1", kind: "escalation", customer_id: "C1",
  intent: "unrecognized_charge", amount_usd: 800, language: "pt",
  reason: "approval.amount_above_auto_register", priority: "normal",
  sla_due_at: "2026-09-30T15:30:00", overdue: false, updated: false,
  status: "pending_analyst_approval", recommended_action: "register_and_offer_block",
  can_approve: true, created_at: "2026-09-30T11:30:00", simulated: false,
};

test("a case the demo created is tagged [simulado] in the queue", () => {
  const r = queueRow({ ...item, simulated: true }, NOW);
  assert.deepEqual(r.tags, [{ text: "[simulado]", tone: "sim" }]);
});

test("a case a customer opened carries no simulated tag", () => {
  assert.deepEqual(queueRow(item, NOW).tags, []);
});

test("the reset dialog says what it empties and keeps Cancelar as the way out", () => {
  const view = demoResetView();
  assert.equal(view.title, "¿Reiniciar el demo?");
  assert.match(view.confirm, /audit log del demo/);
  assert.match(view.confirm, /\[simulado\]/);
  assert.match(view.confirm, /sesiones de los clientes se cierran/);
  assert.equal(view.confirmLabel, "Reiniciar demo");
  assert.equal(view.cancelLabel, "Cancelar");
});

// Supervision on the screens (TRZ-29, TRZ-32, TRZ-35): the audit label, the decision panel of an
// audit sample, the review status, the reason of a rejection, the avatar counter and the switch.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  auditLabel, automationView, caseHeading, dialogKey, decisionDone, decisionPanel, queueRow, reasonLabel,
} from "../../web/assets/analyst-view.js";
import { translator } from "../../web/assets/i18n.js";
import { closedNote, statusKey, statusTone, unreadBadge } from "../../web/assets/view.js";

const es = translator("es");
const pt = translator("pt");

test("an audit sample is labeled with rho as the policy sends it", () => {
  assert.equal(auditLabel(0.1), "Muestra de auditoría (ρ = 0,10)");
  assert.equal(reasonLabel("audit.sample", 0.25), "Muestra de auditoría (ρ = 0,25)");
  const item = {
    queue_id: 1, case_id: "CASE-A", kind: "audit_sample", customer_id: "C1", intent: "unrecognized_charge",
    amount_usd: 20, language: "es", reason: "audit.sample", priority: "normal", sla_due_at: null,
    overdue: false, updated: false, status: "registered_verified", can_approve: true,
  };
  const row = queueRow(item, Date.now(), 0.1);
  assert.equal(row.reason, "Muestra de auditoría (ρ = 0,10)");
  assert.equal(row.kind, "Auditoría");
});

test("an audit sample is confirmed or reversed, never asked about", () => {
  const panel = decisionPanel({ kind: "audit_sample", can_approve: true });
  assert.equal(panel.approveLabel, "Confirmar");
  assert.equal(panel.rejectLabel, "Revertir");
  assert.equal(panel.canAsk, false);
  assert.match(panel.approveHint, /no se anula/);
  assert.equal(decisionPanel({ kind: "escalation", can_approve: true }).rejectLabel, "Rechazar");
});

test("what an audit decision did, in one sentence", () => {
  assert.equal(
    decisionDone({ decision: "approve", status: "registered_verified" }),
    "Auditoría confirmada: nada cambia para el cliente.",
  );
  assert.match(decisionDone({ decision: "reject", status: "in_review" }), /^Revertido: .*no se anula\.$/);
  assert.equal(caseHeading("audit_sample", "in_review", "CASE-A"), "En revisión por una analista · CASE-A");
});

test("a reversed audit reads as in review by an analyst, not as a bank claim in review", () => {
  const dispute = { source: "disputes", status: "in_review" };
  assert.equal(statusKey(dispute), "under_review");
  assert.equal(es("status_under_review"), "En revisión por una analista");
  assert.equal(pt("status_under_review"), "Em revisão por uma analista");
  assert.equal(statusTone(statusKey(dispute)), "warn");
  assert.equal(statusKey({ source: "complaints", status: "in_review" }), "in_review");
});

test("a rejection tells its reason in plain words, in both languages", () => {
  const item = { status: "rejected", reason: "wrong_charge" };
  assert.match(closedNote(es, item), /^Motivo: el cargo identificado no era el correcto\. Revisamos/);
  assert.match(closedNote(pt, item), /^Motivo: a cobrança identificada não era a correta\./);
  assert.equal(closedNote(es, { status: "rejected" }), es("rejectedNote"));
  assert.equal(closedNote(es, { status: "registered" }), null);
});

test("the avatar counts unread notifications", () => {
  assert.deepEqual(unreadBadge(es, 0), { hidden: true, text: "0", title: "0 notificación(es) sin leer" });
  assert.equal(unreadBadge(es, 3).hidden, false);
  assert.equal(unreadBadge(es, 12).text, "9+");
});

test("the switch says its state and asks before it changes", () => {
  const on = automationView({ all_to_human: false });
  assert.equal(on.text, "Automatización activa");
  assert.match(on.confirm, /Automatización desactivada/);
  const off = automationView({ all_to_human: true });
  assert.equal(off.text, "Todo a humano");
  assert.equal(off.tone, "bad");
});

test("both directions of the switch ask in Spanish, with the action as the button", () => {
  const on = automationView({ all_to_human: false });
  assert.deepEqual([on.title, on.confirmLabel, on.cancelLabel], ["¿Mandar todo a humano?", "Mandar todo a humano", "Cancelar"]);
  const off = automationView({ all_to_human: true });
  assert.deepEqual([off.title, off.confirmLabel, off.cancelLabel], ["¿Reactivar la automatización?", "Reactivar la automatización", "Cancelar"]);
});

test("in the dialog Escape cancels and Tab stays on its two buttons", () => {
  assert.deepEqual(dialogKey("Escape", false, 0, 2), { cancel: true });
  assert.deepEqual(dialogKey("Tab", false, 0, 2), { focus: 1 });
  assert.deepEqual(dialogKey("Tab", false, 1, 2), { focus: 0 });
  assert.deepEqual(dialogKey("Tab", true, 0, 2), { focus: 1 });
  assert.deepEqual(dialogKey("Tab", false, -1, 2), { focus: 0 });
  assert.equal(dialogKey("Enter", false, 0, 2), null);
});

test("the console asks with the system's dialog, never the browser's own window", () => {
  // The browser's window has no style and says Cancel and OK in English (QA of TRZ-35).
  const js = readFileSync(new URL("../../web/assets/analyst.js", import.meta.url), "utf8");
  assert.doesNotMatch(js, /window\.(confirm|alert|prompt)\(/);
  const html = readFileSync(new URL("../../web/analista/index.html", import.meta.url), "utf8");
  const dialog = html.slice(html.indexOf('id="switch-dialog"'));
  assert.match(html, /<div class="backdrop" id="switch-dialog" hidden>/);
  assert.match(dialog, /class="card modal" role="dialog" aria-modal="true" aria-labelledby="switch-title"/);
});

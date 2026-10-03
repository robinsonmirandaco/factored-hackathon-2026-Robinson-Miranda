// How Mis aclaraciones names each item (QA finding 3).
import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../../web/assets/i18n.js";
import { clarificationLines } from "../../web/assets/view.js";

const es = translator("es");

test("a case without a charge is named by its intent", () => {
  const item = { id: "CASE-1", source: "cases", status: "escalated", case_id: "CASE-1", intent: "unrecognized_charge" };
  assert.equal(clarificationLines(es, "es", item).title, "Cargo no reconocido");
});

test("a dispute is named by its charge and referenced by its folio and case", () => {
  const item = {
    id: "DSP-2026-00001", source: "disputes", status: "registered", case_id: "CASE-D86BD10EFD",
    folio: "DSP-2026-00001", merchant: "Netflix", amount: 21.84, currency: "USD", charge_at: "2026-06-11T03:43:00",
  };
  const { title, ref } = clarificationLines(es, "es", item);
  assert.match(title, /^Netflix · USD\s21\.84 · 11 jun 2026$/);
  assert.equal(`${title} ${ref}`.split("DSP-2026-00001").length - 1, 1);
});

test("a case with a person shows the review time with its simulated label", async () => {
  const { reviewLine } = await import("../../web/assets/view.js");
  const item = { id: "CASE-1", source: "cases", status: "escalated", case_id: "CASE-1", review_hours: 24 };
  assert.deepEqual(reviewLine(es, item), {
    text: "Plazo de revisión: 24 horas",
    label: "plazo de la política de demostración, no del banco",
  });
  assert.equal(reviewLine(es, { ...item, review_hours: null }), null);
});

test("registered, in review and rejected have different colors", async () => {
  const { statusTone } = await import("../../web/assets/view.js");
  assert.equal(statusTone("registered"), "ok");
  assert.equal(statusTone("approved"), "ok");
  for (const s of ["escalated", "pending_analyst_approval", "failed", "in_review", "received"]) {
    assert.equal(statusTone(s), "warn", s);
  }
  assert.equal(statusTone("rejected"), "bad");
  assert.notEqual(statusTone("registered"), statusTone("escalated"));
});

test("an open question of the analyst shows its deadline and a form (TRZ-28)", async () => {
  const { infoRequestView, openQuestions } = await import("../../web/assets/view.js");
  const item = {
    id: "CASE-1", source: "cases", status: "awaiting_customer", case_id: "CASE-1",
    info_request: { id: 1, question: "¿Hiciste la compra?", asked_on: "2026-06-17", due_on: "2026-06-24", overdue: false, status: "open", answered_at: null },
  };
  assert.deepEqual(infoRequestView(es, "es", item), {
    question: "¿Hiciste la compra?", due: "Responde antes del 24 jun 2026", overdue: false, canAnswer: true,
  });
  const answered = { ...item, info_request: { ...item.info_request, status: "answered" } };
  assert.equal(infoRequestView(es, "es", answered).canAnswer, false);
  assert.equal(openQuestions([item, answered, { id: "X", source: "complaints" }]), 1);
  const pt = translator("pt");
  assert.equal(infoRequestView(pt, "pt", item).due, "Responda até 24 de jun de 2026");
});

test("a case waiting for the customer's answer shows only the deadline to answer", async () => {
  const { deadlineKind } = await import("../../web/assets/view.js");
  const waiting = { source: "cases", status: "awaiting_customer", info_request: { status: "open" } };
  assert.equal(deadlineKind(waiting), "answer");
  assert.equal(deadlineKind({ source: "cases", status: "escalated", review_hours: 4 }), "review");
  assert.equal(deadlineKind({ source: "disputes", due_date: "2026-07-08" }), "due");
  assert.equal(deadlineKind({ source: "cases", status: "escalated", info_request: { status: "answered" } }), "none");
});

test("a rejected clarification explains what the customer can do, without a deadline (QA 2)", async () => {
  const { closedNote, deadlineKind } = await import("../../web/assets/view.js");
  const item = { id: "CASE-1", source: "cases", status: "rejected", case_id: "CASE-1" };
  assert.equal(deadlineKind(item), "closed");
  assert.match(closedNote(es, item), /no procedió/);
  assert.match(closedNote(translator("pt"), item), /não foi aceito/);
  assert.equal(closedNote(es, { ...item, status: "escalated" }), null);
});

test("after answering, the customer sees the answer as the system stored it (QA 5)", async () => {
  const { infoRequestView } = await import("../../web/assets/view.js");
  const item = {
    id: "CASE-1", source: "cases", status: "escalated", case_id: "CASE-1",
    info_request: { id: 1, question: "¿Hiciste la compra?", asked_on: "2026-06-17", due_on: "2026-06-24", overdue: false, status: "answered", answered_at: "2026-09-30T20:00:00", answer: "No, mi correo es [EMAIL]" },
  };
  const view = infoRequestView(es, "es", item);
  assert.equal(view.answer, "No, mi correo es [EMAIL]");
  assert.equal(view.canAnswer, false);
});

test("a closed clarification does not say an analyst is reviewing the answer", async () => {
  const { infoRequestView } = await import("../../web/assets/view.js");
  const item = {
    id: "CASE-1", source: "cases", status: "rejected", case_id: "CASE-1",
    info_request: { id: 1, question: "¿Hiciste la compra?", asked_on: "2026-06-17", due_on: "2026-06-24", overdue: false, status: "answered", answered_at: null, answer: "No" },
  };
  const view = infoRequestView(es, "es", item);
  assert.equal(view.answer, "No");
  assert.equal(view.answered, null);
});

test("the customer sees every question of the analyst with its answer, in order (regression 1)", async () => {
  const { infoRequestViews } = await import("../../web/assets/view.js");
  const q = (id, question, status, answer) => ({ id, question, asked_on: "2026-06-17", due_on: "2026-06-24", overdue: false, status, answered_at: null, answer });
  const item = {
    id: "CASE-1", source: "cases", status: "awaiting_customer", case_id: "CASE-1",
    info_request: q(2, "¿Segunda?", "open", null),
    info_requests: [q(1, "¿Primera?", "answered", "Sí"), q(2, "¿Segunda?", "open", null)],
  };
  const views = infoRequestViews(es, "es", item);
  assert.deepEqual(views.map((v) => [v.question, v.answer ?? null, v.canAnswer]), [
    ["¿Primera?", "Sí", false],
    ["¿Segunda?", null, true],
  ]);
  // Only the open question carries the note that an analyst reviews answers once it is sent.
  assert.equal(views[0].answered, null);
});

test("a case closed for lack of information says so and invites a new message (TRZ-28 CA3)", async () => {
  const { closedNote, deadlineKind } = await import("../../web/assets/view.js");
  const { translator } = await import("../../web/assets/i18n.js");
  const { statusLabel } = await import("../../web/assets/analyst-view.js");
  const item = { source: "cases", status: "closed_no_info", info_request: { status: "expired" } };
  assert.equal(deadlineKind(item), "closed");
  const es = translator("es");
  const pt = translator("pt");
  assert.equal(es("status_closed_no_info"), "Cerrada por falta de información");
  assert.equal(pt("status_closed_no_info"), "Encerrada por falta de informação");
  assert.match(closedNote(es, item), /^No recibimos tu respuesta a tiempo/);
  assert.match(closedNote(pt, item), /^Não recebemos sua resposta a tempo/);
  assert.equal(statusLabel("closed_no_info"), "Cerrado por falta de información");
  const { statusTone } = await import("../../web/assets/view.js");
  assert.equal(statusTone("closed_no_info"), "bad");
});

test("a question or answer the purge replaced is shown in the language of the screen (TRZ-41)", async () => {
  const { infoRequestViews } = await import("../../web/assets/view.js");
  const { translator } = await import("../../web/assets/i18n.js");
  const purged = "[eliminado por retención]";
  const item = { source: "cases", status: "closed_no_info", info_requests: [{ status: "expired", question: purged, answer: purged }] };
  const [pt] = infoRequestViews(translator("pt"), "pt", item);
  assert.equal(pt.question, "[removido por retenção]");
  assert.equal(pt.answer, "[removido por retenção]");
  const [es] = infoRequestViews(translator("es"), "es", item);
  assert.equal(es.question, purged);
  const other = { ...item, info_requests: [{ status: "expired", question: "¿Compraste?", answer: null }] };
  assert.equal(infoRequestViews(translator("pt"), "pt", other)[0].question, "¿Compraste?");
});

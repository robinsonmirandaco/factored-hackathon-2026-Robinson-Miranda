// The trace of a case in the customer's audit view of the demo (TRZ-34 CA4): off by default,
// its route, and the steps shown as the API wrote them, in Spanish and Portuguese.
import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../../web/assets/i18n.js";
import { AUDIT_KEY, auditOn, auditSwitchView, stepDuration, stepTime, traceCase, traceHref, traceRoute, traceView } from "../../web/assets/view.js";

test("the audit view is off until the customer turns it on in this tab", () => {
  assert.equal(AUDIT_KEY, "trazo.customer.audit");
  assert.equal(auditOn(null), false);
  assert.equal(auditOn("off"), false);
  assert.equal(auditOn("on"), true);
});

test("the switch says its state in the language of the screen", () => {
  assert.deepEqual(auditSwitchView(translator("es"), false), { text: "Traza del caso: Apagada", checked: false });
  assert.deepEqual(auditSwitchView(translator("pt"), true), { text: "Trilha do caso: Ligada", checked: true });
});

test("Ver traza opens the route of its case and only that route is a trace", () => {
  const href = traceHref("CASE-AB 1");
  assert.equal(href, "#/traza/CASE-AB%201");
  assert.equal(traceCase(href), "CASE-AB 1");
  assert.equal(traceCase("#/aclaraciones"), null);
  assert.equal(traceCase(""), null);
});

const TRACE = {
  case_id: "CASE-1",
  bank_date: "2026-06-17",
  language: "pt",
  status: "pending_analyst_approval",
  turns: [
    {
      number: 1, kind: "button", header: "Você tocou em «Não reconheço» em uma movimentação",
      steps: [
        { number: 1, id: 7, trace_id: "t-1", actor: "agent", action: "button_press", text: "Linha 1.", offset_ms: 0, duration_ms: null },
        { number: 2, id: 8, trace_id: "t-1", actor: "agent", action: "turn_complete", text: "Linha 2.", offset_ms: 1234, duration_ms: 1400 },
      ],
    },
    {
      number: 2, kind: "recognition", header: "Você disse que não reconhece a cobrança",
      steps: [
        { number: 3, id: 9, trace_id: "t-2", actor: "policy", action: "decide", text: "Linha 3.", offset_ms: null, duration_ms: null },
      ],
    },
  ],
};

test("a step's time is a difference inside its turn and its duration, with the decimal comma", () => {
  assert.equal(stepTime("es", 0), "+0,0 s");
  assert.equal(stepTime("pt", 1234), "+1,2 s");
  assert.equal(stepTime("es", null), null);
  assert.equal(stepDuration("es", 1400), "1,4 s");
  assert.equal(stepDuration("pt", 45), "0,0 s");
  assert.equal(stepDuration("es", null), null);
});

test("the trace has its header and its turns with numbered steps, as the API wrote them", () => {
  const pt = traceView(translator("pt"), "pt", TRACE);
  assert.deepEqual(pt.meta, ["Data do banco: 17 de jun de 2026", "Português", "Em análise antes do registro"]);
  assert.deepEqual(pt.turns.map((turn) => [turn.number, turn.header]), [
    [1, "Você tocou em «Não reconheço» em uma movimentação"],
    [2, "Você disse que não reconhece a cobrança"],
  ]);
  assert.deepEqual(pt.turns[0].steps, [
    { number: 1, text: "Linha 1.", traceId: "t-1", time: "+0,0 s", duration: null },
    { number: 2, text: "Linha 2.", traceId: "t-1", time: "+1,2 s", duration: "1,4 s" },
  ]);
  // QA: "policy · decide" under each line was an internal code the customer does not read.
  assert.equal(JSON.stringify(pt).includes("decide"), false);
  assert.equal(pt.turns[1].steps[0].time, null);
  const es = traceView(translator("es"), "es", { ...TRACE, language: "es", status: "registered_verified" });
  assert.equal(es.meta[0].startsWith("Fecha del banco: "), true);
  assert.deepEqual(es.meta.slice(1), ["Español", "Registrada"]);
  assert.deepEqual(traceView(translator("es"), "es", undefined).turns, []);
});

test("every case status has its text in both languages for the trace header", () => {
  const statuses = ["open", "identifying", "recognizing", "recognized_closed", "awaiting_confirmation", "registered_verified", "failed", "pending_analyst_approval", "escalated", "security_blocked", "abstained", "closed", "approved", "rejected", "expired", "awaiting_customer", "closed_no_info"];
  for (const lang of ["es", "pt"]) {
    const t = translator(lang);
    for (const status of statuses) assert.notEqual(t(`status_${status}`), `status_${status}`, `${lang}.${status}`);
  }
});

test("every text of the trace exists in Spanish and Portuguese", () => {
  const keys = ["auditSwitch", "auditSwitchOn", "auditSwitchOff", "traceTitle", "traceNote", "traceNoCase", "traceEmpty", "seeTrace", "backClarifications", "bankDate", "language_es", "language_pt", "turn"];
  for (const lang of ["es", "pt"]) {
    const t = translator(lang);
    for (const key of keys) assert.notEqual(t(key), key, `${lang}.${key}`);
  }
  assert.notEqual(translator("pt")("seeTrace"), translator("es")("seeTrace"));
  assert.match(translator("es")("traceNote"), /^\[simulado\]/);
  assert.match(translator("pt")("traceNote"), /^\[simulado\]/);
});

test("with the audit view off, a trace route goes back to Mis aclaraciones without asking", () => {
  // QA CP-13: the route must never reach the API while the switch is off.
  assert.deepEqual(traceRoute("#/traza/CASE-1", false), { redirect: "#/aclaraciones" });
  assert.deepEqual(traceRoute("#/traza/CASE-1", true), { caseId: "CASE-1" });
  assert.equal(traceRoute("#/aclaraciones", false), null);
});

test("a case the trace cannot find goes back to Mis aclaraciones, not to a new conversation", () => {
  // QA CP-13: "#/traza/CASO" answered 404 case_not_found and the screen offered a new chat.
  assert.deepEqual(traceRoute("#/traza/CASO", true, { status: 404, code: "case_not_found" }), { redirect: "#/aclaraciones" });
  assert.equal(traceRoute("#/traza/CASO", true, { status: 503, code: "db_unavailable" }), null);
});

// The trace of a case in the customer's audit view of the demo (TRZ-34 CA4): off by default,
// its route, and the steps shown as the API wrote them, in Spanish and Portuguese.
import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../../web/assets/i18n.js";
import { AUDIT_KEY, auditOn, auditSwitchView, traceCase, traceHref, traceLines, traceRoute } from "../../web/assets/view.js";

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

test("the steps are shown as the API wrote them, without internal codes", () => {
  const steps = [{
    id: 7, trace_id: "t-1", actor: "policy", action: "decide",
    text: "A política v2026.09.5, regra «approval.autonomy_a1», decidiu pedir a aprovação de uma analista (nível L3).",
  }];
  // QA: "policy · decide" under each line was an internal code the customer does not read.
  assert.deepEqual(traceLines(steps), [{ text: steps[0].text, traceId: "t-1" }]);
  assert.deepEqual(traceLines(undefined), []);
});

test("every text of the trace exists in Spanish and Portuguese", () => {
  const keys = ["auditSwitch", "auditSwitchOn", "auditSwitchOff", "traceTitle", "traceNote", "traceNoCase", "traceEmpty", "seeTrace", "backClarifications"];
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

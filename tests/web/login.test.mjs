// The login speaks the screen's language and asks for the code first (QA findings 5 and 6).
import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../../web/assets/i18n.js";
import { codeStep, errorText } from "../../web/assets/view.js";

const es = translator("es");
const pt = translator("pt");

test("an API error is said in the screen's language, never in the API's English", () => {
  const error = { code: "invalid_code", message: "The code is not valid for this document." };
  assert.equal(errorText(es, error), "El código no es válido para este documento.");
  assert.equal(errorText(pt, error), "O código não é válido para este documento.");
  for (const code of ["document_locked", "code_requests_limited", "validation_error", "db_unavailable", "session_expired"]) {
    assert.notEqual(errorText(es, { code, message: "x" }), "x", code);
    assert.notEqual(errorText(es, { code }), errorText(pt, { code }), code);
  }
});

test("an unknown error falls back to the generic message, not the English one", () => {
  assert.equal(errorText(es, { code: "something_new", message: "English text" }), es("errorGeneric"));
});

test("the code field waits for a requested code, for that same document", () => {
  const doc = { type: "Pasaporte", number: "SYN0000001" };
  assert.deepEqual(codeStep(null, doc), { showCode: false });
  assert.deepEqual(codeStep(doc, doc), { showCode: true });
  assert.deepEqual(codeStep(doc, { ...doc, number: "SYN0000002" }), { showCode: false });
  assert.deepEqual(codeStep(doc, { ...doc, type: "CE" }), { showCode: false });
});

test("Send is enabled only with something to send", async () => {
  const { canSend } = await import("../../web/assets/view.js");
  assert.equal(canSend(""), false);
  assert.equal(canSend("   "), false);
  assert.equal(canSend("hola"), true);
});

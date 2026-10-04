// While a reply is on its way, and when a message could not be sent (improvements after the QA of
// the hardening): a typing bubble, and a sent bubble marked with a retry.
import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../../web/assets/i18n.js";
import { outgoingNote, typingView } from "../../web/assets/view.js";

const es = translator("es");
const pt = translator("pt");

test("the typing bubble names what is happening for screen readers, in both languages", () => {
  // The word is shown next to the animated dots; the label, with its dots, is what is read out.
  assert.deepEqual(typingView(es), { word: "escribiendo", label: "escribiendo...", dots: 3 });
  assert.deepEqual(typingView(pt), { word: "digitando", label: "digitando...", dots: 3 });
});

test("a message being sent or sent carries no note", () => {
  assert.equal(outgoingNote(es, "sending"), null);
  assert.equal(outgoingNote(es, "sent"), null);
});

test("a message that could not be sent is marked and offers a retry, in both languages", () => {
  assert.deepEqual(outgoingNote(es, "failed"), { text: "No se envió", retry: "Reintentar" });
  assert.deepEqual(outgoingNote(pt, "failed"), { text: "Não enviado", retry: "Tentar de novo" });
});

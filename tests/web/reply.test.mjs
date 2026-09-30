// A [simulado] line of a reply is shown as a label, not as running text (QA finding 9).
import assert from "node:assert/strict";
import { test } from "node:test";

import { replyLines } from "../../web/assets/view.js";

test("the policy citation line becomes a simulated label", () => {
  const reply = "Registramos tu aclaración con el folio DSP-2026-00001. Plazo de respuesta: a más tardar el 8 de julio de 2026 (15 días hábiles).\n[simulado] §2.1 · política de demostración, no del banco";
  assert.deepEqual(replyLines(reply), [
    { text: "Registramos tu aclaración con el folio DSP-2026-00001. Plazo de respuesta: a más tardar el 8 de julio de 2026 (15 días hábiles).", simulated: false },
    { text: "§2.1 · política de demostración, no del banco", simulated: true },
  ]);
});

test("a reply without labels is one line", () => {
  assert.deepEqual(replyLines("Hola."), [{ text: "Hola.", simulated: false }]);
});

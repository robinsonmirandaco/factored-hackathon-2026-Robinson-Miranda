// The question and buttons of a pending action (QA finding 2).
import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../../web/assets/i18n.js";
import { pendingButtons, pendingQuestion } from "../../web/assets/view.js";

const es = translator("es");
const pt = translator("pt");
const charge = { merchant: "Netflix", amount: 21.84, currency: "USD", last4: "4821" };

test("the registration question names the amount and the merchant", () => {
  const q = pendingQuestion(es, "es", { action: "register_and_offer_block", ...charge });
  assert.match(q, /¿Registramos la aclaración por USD\s21\.84 de Netflix\?/);
});

test("the offered block has its own question with the card", () => {
  assert.match(pendingQuestion(es, "es", { action: "block", ...charge }), /•••• 4821/);
  assert.match(pendingQuestion(pt, "pt", { action: "block", ...charge }), /•••• 4821/);
});

test("every offer can be confirmed or declined", () => {
  for (const action of ["register", "register_and_offer_block", "register_and_block", "block"]) {
    const kinds = pendingButtons(es, { action, ...charge }).map((b) => b.kind);
    assert.deepEqual(kinds, ["confirm", "decline"], action);
  }
  const block = pendingButtons(es, { action: "block", ...charge });
  assert.equal(block[0].label, "Bloquear la tarjeta");
  assert.equal(block[1].label, "No, gracias");
});

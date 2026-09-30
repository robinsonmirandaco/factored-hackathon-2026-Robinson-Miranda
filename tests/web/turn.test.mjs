// The charge card and buttons of a turn follow the screen's language (QA of TRZ-34).
import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../../web/assets/i18n.js";
import { turnModel } from "../../web/assets/view.js";

// A recognition turn as the API returned it while the screen was in Spanish.
const turn = {
  case_id: "CASE-1",
  trace_id: "t1",
  reply: "Este es el cargo que encontramos; revisa el detalle.\n¿Reconoces este cargo?",
  clues: [],
  charge: {
    transaction_id: "TX1", transaction_type: "Purchase", merchant: "Netflix", amount: 21.84,
    currency: "USD", converted_amount: null, converted_currency: "MXN", at: "2026-06-11T03:43:00",
    channel: "Web", city: "Bogotá", product_type: "credit_card", last4: "4821", status: "Approved",
  },
  choices: [
    { id: "not_recognized", label: "Sigo sin reconocerlo" },
    { id: "recognized", label: "Ya lo reconozco" },
  ],
  options: [],
  claims: [],
  pending_action: null,
  dispute_folio: null,
};

test("switching to Portuguese translates the card and the buttons, not the reply", () => {
  const pt = turnModel(translator("pt"), "pt", turn);
  assert.deepEqual(pt.buttons.map((b) => b.label), ["Continuo sem reconhecer", "Já reconheço"]);
  assert.equal(pt.buttons[0].primary, true);
  const fields = Object.fromEntries(pt.card.map((row) => [row.label, row.value]));
  assert.equal(fields["Estabelecimento"], "Netflix");
  assert.equal(fields["Canal"], "Compra on-line");
  assert.equal(fields["Produto"], "Cartão de crédito •••• 4821");
  assert.equal(pt.reply, turn.reply);
});

test("the pending question follows the language too", () => {
  const pending = { ...turn, charge: null, choices: [], pending_action: {
    action_id: "ACT-0123456789", action: "register", merchant: "Netflix", amount: 21.84, currency: "USD", last4: "4821",
  } };
  const es = turnModel(translator("es"), "es", pending);
  const pt = turnModel(translator("pt"), "pt", pending);
  assert.match(es.question, /^¿Registramos/);
  assert.match(pt.question, /^Registramos a contestação/);
  assert.deepEqual(pt.buttons.map((b) => b.label), ["Sim, registrar a contestação", "Não, prefiro não"]);
});

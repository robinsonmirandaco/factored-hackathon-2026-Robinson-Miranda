// Seen in the demo rehearsal: the chip said "Monto 2.3e+06 COP", the dossier gave the date as the
// customer's words ("ontem") and recommended a registration on a case with no charge.
import assert from "node:assert/strict";
import { test } from "node:test";

import { clueChips, recommendationText } from "../../web/assets/analyst-view.js";
import { translator } from "../../web/assets/i18n.js";
import { chipParts } from "../../web/assets/view.js";

const NBSP = " ";
const amount = { field: "amount", value: "2300000.00 COP", evidence: "2.300.000 pesos", amount: 2300000, currency: "COP" };

test("an amount chip uses the money format of the screen's language", () => {
  assert.equal(chipParts(translator("es"), "es", amount).value, `COP${NBSP}2,300,000.00`);
  assert.equal(chipParts(translator("pt"), "pt", amount).value, `COP${NBSP}2.300.000,00`);
  assert.equal(chipParts(translator("pt"), "pt", amount).evidence, "2.300.000 pesos");
});

test("an amount chip without currency is a plain number of the screen's language", () => {
  const plain = { ...amount, value: "2300000.00", currency: null };
  assert.equal(chipParts(translator("es"), "es", plain).value, "2,300,000");
  assert.equal(chipParts(translator("pt"), "pt", plain).value, "2.300.000");
});

test("the dossier gives the understood date as days, with the customer's words as evidence", () => {
  const [date] = clueChips([{
    field: "date",
    value: { expression: "ontem", window_days: [1, 1], window_from: "2026-06-16", window_to: "2026-06-16" },
    evidence: "ontem",
    read_in: 1,
  }]);
  assert.equal(date.value, "16 jun y días cercanos");
  assert.equal(date.evidence, "ontem");
});

test("a case with no identified charge says why nothing is recommended", () => {
  assert.equal(
    recommendationText({ recommended_action: null, charge_identified: false, case_kind: "escalation" }),
    "Sin acción recomendada: no se identificó ningún cargo.",
  );
  assert.equal(
    recommendationText({ recommended_action: null, charge_identified: false, case_kind: "security_event" }),
    "Sin acción recomendada.",
  );
  assert.equal(
    recommendationText({ recommended_action: "register_and_offer_block", charge_identified: true, case_kind: "escalation" }),
    "Registrar y ofrecer el bloqueo de la tarjeta",
  );
});

// What a chip says (QA finding 4): what was understood, then the literal fragment.
import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../../web/assets/i18n.js";
import { chipParts } from "../../web/assets/view.js";

const es = translator("es");

test("a date chip shows the window, not the phrase again", () => {
  const chip = { field: "date", value: "la semana pasada", evidence: "de la semana pasada", window_from: "2026-06-08", window_to: "2026-06-14" };
  assert.deepEqual(chipParts(es, "es", chip), { label: "Fecha", value: "8 jun a 14 jun", evidence: "de la semana pasada" });
});

test("a one-day window is one date", () => {
  const chip = { field: "date", value: "ayer", evidence: "ayer", window_from: "2026-06-16", window_to: "2026-06-16" };
  assert.equal(chipParts(es, "es", chip).value, "16 jun");
});

test("a fragment equal to the value is not repeated", () => {
  const chip = { field: "merchant_hint", value: "Netflix", evidence: "netflix", window_from: null, window_to: null };
  assert.deepEqual(chipParts(es, "es", chip), { label: "Comercio", value: "Netflix", evidence: null });
});

test("card in possession is said in words", () => {
  const chip = { field: "card_in_possession", value: "yes", evidence: "tengo la tarjeta", window_from: null, window_to: null };
  assert.equal(chipParts(es, "es", chip).value, "la tienes");
});

// The Estado de autonomía tab (TRZ-31): thresholds, one row per cell, the reversed cases in two
// groups, and no real-clock date.
import assert from "node:assert/strict";
import { test } from "node:test";

import { SIMULATED_NOTE, autonomyRow, reversedGroups, thresholdsText } from "../../web/assets/analyst-view.js";

const THRESHOLDS = {
  window_n: 20, z: 1.645, demote_if_wilson_lower_gte: 0.3, promote_if_rate_lt: 0.15,
  promote_after_consecutive_windows: 2, audit_sample_rate: 0.1,
};
const BLOCK = {
  audit_id: 41, closed_by: "CASE-PT20", n: 20, reversals: 10, r: 0.5, w: 0.3274,
  threshold: "demote_if_wilson_lower_gte", threshold_value: 0.3, level_before: "A0",
  level_after: "A1", changed: true,
};
const open = {
  intent: "unrecognized_charge", language: "pt", level: "A0", block_reviews: 19,
  block_reversals: 9, rate: 0.4737, last_block: null, last_change: null,
  reversed_last_block: [],
  reversed_open_block: [{ case_id: "CASE-S1", reason: "wrong_charge", simulated: true }],
};
const closed = {
  ...open, level: "A1", block_reviews: 0, block_reversals: 0, rate: null,
  last_block: BLOCK, last_change: BLOCK,
  reversed_last_block: [
    { case_id: "CASE-S1", reason: "wrong_charge", simulated: true },
    { case_id: "CASE-PT20", reason: "should_not_act", simulated: false },
  ],
  reversed_open_block: [],
};

test("the thresholds are read from the policy and written as the story asks", () => {
  assert.deepEqual(thresholdsText(THRESHOLDS), [
    "N = 20", "Baja si W ≥ 0,30", "Sube con dos bloques seguidos con r < 0,15", "z = 1,645",
  ]);
});

test("an open block has r but no W: W is computed with N reviews", () => {
  const r = autonomyRow(open, THRESHOLDS);
  assert.equal(r.cell, "Cargo no reconocido · PT");
  assert.equal(r.level, "A0");
  assert.equal(r.levelText, "Autónomo con confirmación del cliente");
  assert.equal(r.reviews, "19 de 20");
  assert.equal(r.reversals, "9");
  assert.equal(r.rate, "0,47");
  assert.equal(r.w, "Se calcula con N = 20");
  assert.equal(r.change, "Sin cambios de nivel");
  assert.equal(r.reversedCount, 1);
});

test("after the block that changed the level the row shows its W and the last change", () => {
  const r = autonomyRow(closed, THRESHOLDS);
  assert.equal(r.levelText, "Requiere aprobación de analista");
  assert.equal(r.rate, "Sin revisiones");
  assert.equal(r.w, "0,327");
  assert.equal(r.change, "A0 → A1 con 10 de 20, r = 0,50, W = 0,327 ≥ 0,30");
  assert.equal(r.changeCase, "CASE-PT20");
});

test("the reversed cases come in two groups, the closed block first", () => {
  const [last, current] = reversedGroups(closed);
  assert.equal(last.title, "Último bloque cerrado (10 de 20, produjo el cambio de A0 a A1)");
  assert.deepEqual(last.items.map((i) => [i.caseId, i.reason, i.simulated, i.href]), [
    ["CASE-S1", "Cargo equivocado", true, "#/caso/CASE-S1"],
    ["CASE-PT20", "No debía actuar", false, "#/caso/CASE-PT20"],
  ]);
  assert.equal(current.title, "Bloque abierto (0 de 0 revisiones)");
  assert.deepEqual(current.items, []);
});

test("without a closed block only the open block is listed", () => {
  const groups = reversedGroups(open);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].title, "Bloque abierto (9 de 19 revisiones)");
});

test("a closed block that kept the level says so", () => {
  const kept = { ...BLOCK, reversals: 9, w: 0.2841, threshold: null, level_after: "A0", changed: false };
  const [last] = reversedGroups({ ...closed, level: "A0", last_block: kept, last_change: null });
  assert.equal(last.title, "Último bloque cerrado (9 de 20, la celda siguió en A0)");
});

test("no text of the tab carries a date", () => {
  const texts = [
    ...thresholdsText(THRESHOLDS),
    ...Object.values(autonomyRow(closed, THRESHOLDS)).map(String),
    ...reversedGroups(closed).map((g) => g.title),
    SIMULATED_NOTE,
  ];
  for (const text of texts) assert.doesNotMatch(text, /\d{4}-\d{2}-\d{2}|\d{1,2}\/\d{1,2}\/\d{4}/);
});

test("the simulated note is the sentence of the story", () => {
  assert.equal(SIMULATED_NOTE, "Reversiones simuladas contra verdad de terreno en la evaluación");
});

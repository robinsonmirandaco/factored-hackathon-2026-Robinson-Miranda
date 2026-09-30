// How Mis aclaraciones names each item (QA finding 3).
import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../../web/assets/i18n.js";
import { clarificationLines } from "../../web/assets/view.js";

const es = translator("es");

test("a case without a charge is named by its intent", () => {
  const item = { id: "CASE-1", source: "cases", status: "escalated", case_id: "CASE-1", intent: "unrecognized_charge" };
  assert.equal(clarificationLines(es, "es", item).title, "Cargo no reconocido");
});

test("a dispute is named by its charge and referenced by its folio and case", () => {
  const item = {
    id: "DSP-2026-00001", source: "disputes", status: "registered", case_id: "CASE-D86BD10EFD",
    folio: "DSP-2026-00001", merchant: "Netflix", amount: 21.84, currency: "USD", charge_at: "2026-06-11T03:43:00",
  };
  const { title, ref } = clarificationLines(es, "es", item);
  assert.match(title, /^Netflix · USD\s21\.84 · 11 jun 2026$/);
  assert.equal(`${title} ${ref}`.split("DSP-2026-00001").length - 1, 1);
});

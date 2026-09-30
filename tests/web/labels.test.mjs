// Plain words for codes of the records, one date format, and a button that names its charge
// (QA findings 7, 8 and 10).
import assert from "node:assert/strict";
import { test } from "node:test";

import { dayTime, label, translator } from "../../web/assets/i18n.js";
import { buttonMessage } from "../../web/assets/view.js";

const es = translator("es");
const pt = translator("pt");

test("channels are said in words, in both languages", () => {
  assert.equal(label(es, "channel", "Web"), "Compra en línea");
  assert.equal(label(es, "channel", "POS"), "Terminal en comercio");
  assert.equal(label(es, "channel", "App"), "App");
  assert.equal(label(pt, "channel", "Web"), "Compra on-line");
  for (const code of ["ATM", "App", "Branch", "POS", "Transfer", "Web"]) {
    assert.notEqual(label(es, "channel", code), code === "App" ? "" : code, code);
    assert.notEqual(label(pt, "channel", code), code === "App" ? "" : code, code);
  }
});

test("product statuses are said in words", () => {
  for (const code of ["Active", "Blocked", "Closed", "Suspended"]) {
    assert.notEqual(label(es, "pstatus", code), code);
    assert.notEqual(label(pt, "pstatus", code), code);
  }
  assert.equal(label(es, "pstatus", "Active"), "Activa");
});

test("date and time read the same as the chat text: 24 hours, no a.m.", () => {
  assert.equal(dayTime("es", "2026-06-11T03:43:00"), "11 jun 2026, 03:43");
  assert.equal(dayTime("es", "2026-06-11T15:05:00"), "11 jun 2026, 15:05");
});

test("the button's message names the charge it was pressed on", () => {
  const netflix = { merchant: "Netflix", at: "2026-06-11T03:43:00", status: "Approved", transaction_type: "Purchase" };
  assert.equal(buttonMessage(es, "es", netflix), "No reconozco el cargo de Netflix del 11 jun");
  assert.equal(buttonMessage(pt, "pt", netflix), "Não reconheço a cobrança de Netflix do dia 11 de jun");
  const pending = { ...netflix, merchant: "Rappi", status: "Pending" };
  assert.equal(buttonMessage(es, "es", pending), "¿Qué es el cargo de Rappi del 11 jun?");
  const atm = { merchant: null, at: "2026-06-11T03:43:00", status: "Approved", transaction_type: "Withdrawal" };
  assert.equal(buttonMessage(es, "es", atm), "No reconozco el cargo de Retiro del 11 jun");
});

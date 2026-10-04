// The opening of the chat when the tab's conversation is already with a person (QA of TRZ-34):
// after a reload or in a duplicated tab it says so instead of greeting as if nothing were open.
import assert from "node:assert/strict";
import { test } from "node:test";

import { translator } from "../../web/assets/i18n.js";
import { chatOpening } from "../../web/assets/view.js";

const es = translator("es");
const pt = translator("pt");
const withPerson = { id: "CASE-1", source: "cases", status: "escalated", case_id: "CASE-1", review_hours: 24 };

test("a tab with no case greets as usual", () => {
  assert.deepEqual(chatOpening(es, null, [withPerson]), { text: es("chatIntro"), review: null });
});

test("a case with a person is named, with its review time and its label", () => {
  for (const status of ["escalated", "pending_analyst_approval", "failed"]) {
    const opening = chatOpening(es, "CASE-1", [{ ...withPerson, status }]);
    assert.notEqual(opening.text, es("chatIntro"), status);
    assert.match(opening.text, /CASE-1/);
    assert.match(opening.text, /persona/);
    assert.deepEqual(opening.review, {
      text: "Plazo de revisión: 24 horas",
      label: "plazo de la política de demostración, no del banco",
    });
  }
});

test("the opening speaks the language of the screen", () => {
  const opening = chatOpening(pt, "CASE-1", [withPerson]);
  assert.match(opening.text, /CASE-1/);
  assert.match(opening.text, /pessoa/);
});

test("a case no longer with a person, or not listed, greets as usual", () => {
  for (const status of ["approved", "rejected", "closed_no_info", "awaiting_customer"]) {
    assert.equal(chatOpening(es, "CASE-1", [{ ...withPerson, status }]).text, es("chatIntro"), status);
  }
  assert.equal(chatOpening(es, "CASE-2", [withPerson]).text, es("chatIntro"));
  assert.equal(chatOpening(es, "CASE-1", []).text, es("chatIntro"));
});

test("a dispute of the same case is not a case with a person", () => {
  const dispute = { ...withPerson, source: "disputes", status: "registered", id: "DSP-2026-00001" };
  assert.equal(chatOpening(es, "CASE-1", [dispute]).text, es("chatIntro"));
});

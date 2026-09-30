// "Nueva conversación" starts a new case even while an answer of the old one is on its way
// (QA: the late answer set the old case back, and the next message went to a case with a person).
import assert from "node:assert/strict";
import { test } from "node:test";

import { createConversation } from "../../web/assets/view.js";

test("an answer of a conversation that was left is not applied", () => {
  const talk = createConversation();
  const sentIn = talk.current();
  talk.startNew();
  assert.equal(talk.isCurrent(sentIn), false);
  assert.equal(talk.isCurrent(talk.current()), true);
});

test("each new conversation leaves the previous one", () => {
  const talk = createConversation();
  const first = talk.current();
  const second = talk.startNew();
  const third = talk.startNew();
  assert.deepEqual([talk.isCurrent(first), talk.isCurrent(second), talk.isCurrent(third)], [false, false, true]);
});

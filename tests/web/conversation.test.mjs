// "Nueva conversación" starts a new case even while an answer of the old one is on its way
// (QA: the late answer set the old case back, and the next message went to a case with a person).
import assert from "node:assert/strict";
import { test } from "node:test";

import { composerState, createConversation } from "../../web/assets/view.js";

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

// Seen in the demo rehearsal: a message written while a chat request was in flight was cleared
// and never sent. One request at a time; the typed text stays until it was really sent.
test("a second request cannot start while one is in flight", () => {
  const talk = createConversation();
  const first = talk.begin();
  assert.notEqual(first, null);
  assert.equal(talk.busy(), true);
  assert.equal(talk.begin(), null);
  talk.end(first, true);
  assert.equal(talk.busy(), false);
  assert.notEqual(talk.begin(), null);
});

test("the typed text is cleared only when its request was sent and answered", () => {
  const talk = createConversation();
  assert.equal(talk.end(talk.begin(), true), true);
  assert.equal(talk.end(talk.begin(), false), false);
});

test("a new conversation frees the chat and ignores the old answer", () => {
  const talk = createConversation();
  const old = talk.begin();
  talk.startNew();
  assert.equal(talk.busy(), false);
  const fresh = talk.begin();
  assert.equal(talk.end(old, true), false);
  assert.equal(talk.busy(), true);
  talk.end(fresh, true);
  assert.equal(talk.busy(), false);
});

test("Send is off and the reply is shown on its way while a request is in flight", () => {
  assert.deepEqual(composerState("hola", true), { canSubmit: false, pending: true });
  assert.deepEqual(composerState("hola", false), { canSubmit: true, pending: false });
  assert.deepEqual(composerState("   ", false), { canSubmit: false, pending: false });
});

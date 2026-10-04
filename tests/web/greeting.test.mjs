// The greeting of the chat follows the language of the screen until the customer writes (QA of
// the hardening): after that, what is in the chat stays as it was written.
import assert from "node:assert/strict";
import { test } from "node:test";

import { greetingFollowsLanguage } from "../../web/assets/view.js";

test("with no message of the customer the greeting is written again in the new language", () => {
  assert.equal(greetingFollowsLanguage(0), true);
});

test("once the customer wrote, the chat keeps what was written", () => {
  assert.equal(greetingFollowsLanguage(1), false);
  assert.equal(greetingFollowsLanguage(3), false);
});

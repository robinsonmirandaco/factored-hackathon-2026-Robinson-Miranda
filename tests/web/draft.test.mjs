// What the chat keeps between sessions (QA of the hardening): another person signing in on the
// same browser must not see what the previous one typed.
import assert from "node:assert/strict";
import { test } from "node:test";

import { draftOn } from "../../web/assets/view.js";

const typed = "No reconozco un cargo, mi número es 5512345678";

test("a draft is kept when a send fails and dropped when a session ends or starts", () => {
  assert.equal(draftOn("send_failed", typed), typed);
  assert.equal(draftOn("logout", typed), "");
  assert.equal(draftOn("login", typed), "");
});

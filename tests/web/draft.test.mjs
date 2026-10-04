// What the chat keeps between sessions (QA of the hardening): another person signing in on the
// same browser must not see what the previous one typed.
import assert from "node:assert/strict";
import { test } from "node:test";

import { draftOn } from "../../web/assets/view.js";

const typed = "No reconozco un cargo, mi número es 5512345678";

test("the field is left empty on send, sign out and sign in; a failed send keeps the text in its bubble", () => {
  assert.equal(draftOn("send", typed), "");
  assert.equal(draftOn("logout", typed), "");
  assert.equal(draftOn("login", typed), "");
  // Anything else leaves what is typed alone.
  assert.equal(draftOn("language", typed), typed);
});

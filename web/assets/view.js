// What the customer screens say, built from API data. Pure functions, no DOM: the chat and the
// lists render their output, and tests/web checks it.

import { money } from "./i18n.js";

const card = (p) => (p.last4 ? `•••• ${p.last4}` : "");

export function pendingQuestion(t, lang, pending) {
  const vars = {
    amount: pending.amount != null ? money(lang, pending.amount, pending.currency) : "",
    merchant: pending.merchant || "",
    card: card(pending),
  };
  if (pending.action === "block") return t("askBlock", vars);
  if (pending.action === "register_and_block") return t("askRegisterAndBlock", vars);
  return t("askRegister", vars);
}

export function pendingButtons(t, pending) {
  const confirm = {
    block: "confirmBlock",
    register_and_block: "confirmRegisterAndBlock",
  }[pending.action] || "confirmRegister";
  return [
    { kind: "confirm", label: t(confirm), primary: true },
    { kind: "decline", label: t("noThanks"), primary: false },
  ];
}

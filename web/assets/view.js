// What the customer screens say, built from API data. Pure functions, no DOM: the chat and the
// lists render their output, and tests/web checks it.

import { day, money } from "./i18n.js";

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

// The two lines of an item of Mis aclaraciones: what it is about, and how to refer to it. Each
// id appears once.
export function clarificationLines(t, lang, item) {
  if (item.source === "complaints") return { title: t("bankRecord"), ref: item.id };
  const charge = [
    item.merchant,
    item.amount != null ? money(lang, item.amount, item.currency) : null,
    item.charge_at ? day(lang, item.charge_at) : null,
  ].filter(Boolean);
  const topic = t(`topic_${item.intent}`);
  const title = charge.length ? charge.join(" · ") : topic.startsWith("topic_") ? t("topic_other") : topic;
  const refs = [];
  if (item.folio) refs.push(`${t("folio")} ${item.folio}`);
  if (item.case_id) refs.push(`${t("caseLabel")} ${item.case_id}`);
  return { title, ref: refs.join(" · ") };
}

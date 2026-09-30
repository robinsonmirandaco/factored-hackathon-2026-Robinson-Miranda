// What the customer screens say, built from API data. Pure functions, no DOM: the chat and the
// lists render their output, and tests/web checks it.

import { day, dayMonth, label, money } from "./i18n.js";

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

// A chip: what was understood, and the literal fragment it came from when it adds something.
export function chipParts(t, lang, chip) {
  let value = chip.value;
  if (chip.field === "card_in_possession") value = t(chip.value === "yes" ? "card_yes" : "card_no");
  if (chip.field === "date" && chip.window_from && chip.window_to) {
    const from = dayMonth(lang, chip.window_from);
    const to = dayMonth(lang, chip.window_to);
    value = from === to ? from : t("dateRange", { from, to });
  }
  const same = chip.evidence.trim().toLowerCase() === String(value).trim().toLowerCase();
  return { label: t(`clue_${chip.field}`), value, evidence: same ? null : chip.evidence };
}

// An API error in the screen's language. The API's message is English, for developers: it is
// never shown; an unknown code gets the generic message.
export function errorText(t, error) {
  const key = `error_${error?.code}`;
  const text = t(key);
  return text === key ? t("errorGeneric") : text;
}

// The code field appears only once a code was requested for the document typed now.
export function codeStep(requestedFor, current) {
  const same = requestedFor !== null
    && requestedFor.type === current.type
    && requestedFor.number === current.number;
  return { showCode: same };
}

// What the customer "says" when pressing a movement's button: the charge it was pressed on.
export function buttonMessage(t, lang, movement) {
  const what = movement.merchant || label(t, "type", movement.transaction_type);
  const key = movement.status === "Pending" ? "btnWhatIs" : "btnNotRecognized";
  return t(key, { what, date: dayMonth(lang, movement.at) });
}

// What the customer screens say, built from API data. Pure functions, no DOM: the chat and the
// lists render their output, and tests/web checks it.

import { day, dayMonth, dayTime, label, money } from "./i18n.js";

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
    // The search has no hard edge: days around the window score lower but can still match.
    value = t("nearDays", { window: from === to ? from : t("dateRange", { from, to }) });
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

// The lines of a reply; a line that starts with [simulado] is a label of demo content (the
// policy citation), shown apart from the text.
export function replyLines(reply) {
  return String(reply).split("\n").filter((line) => line.trim()).map((line) => {
    const simulated = line.startsWith("[simulado]");
    return { text: simulated ? line.slice("[simulado]".length).trim() : line, simulated };
  });
}

// Everything a chat turn shows, in the screen's language: the reply stays as the API wrote it,
// while the chips, the charge card, the buttons and the question are built here, so switching
// the language redraws them. Each button carries the answer it sends.
export function turnModel(t, lang, r) {
  const buttons = [];
  r.choices.forEach((c, i) => {
    const text = t(`choice_${c.id}`) === `choice_${c.id}` ? c.label : t(`choice_${c.id}`);
    // The primary choice comes first: "Sigo sin reconocerlo" (design 10.2, rule 4).
    buttons.push({ label: text, primary: i === 0, answer: { recognition: c.id } });
  });
  for (const o of r.options) {
    const text = `${o.merchant || ""} · ${money(lang, o.amount, o.currency)} · ${day(lang, o.date)}`;
    buttons.push({ label: text, primary: false, answer: { option: o.transaction_id } });
  }
  if (r.options.length) buttons.push({ label: t("noneOfThese"), primary: false, answer: { option: "none" } });
  for (const c of r.claims) {
    buttons.push({ label: `${c.claim_id} · ${day(lang, c.opened_on)}`, primary: false, answer: { option: c.claim_id } });
  }
  let question = null;
  if (r.pending_action) {
    const p = r.pending_action;
    const field = { confirm: "confirm_action_id", decline: "decline_action_id" };
    question = pendingQuestion(t, lang, p);
    for (const b of pendingButtons(t, p)) {
      buttons.push({ label: b.label, primary: b.primary, answer: { [field[b.kind]]: p.action_id } });
    }
  }
  return {
    reply: r.reply,
    lines: replyLines(r.reply),
    chips: (r.clues || []).map((c) => ({ ...chipParts(t, lang, c), title: c.evidence })),
    card: r.charge ? chargeRows(t, lang, r.charge) : [],
    question,
    buttons,
    folio: r.dispute_folio,
  };
}

function chargeRows(t, lang, c) {
  const amount = money(lang, c.amount, c.currency);
  const approx = c.converted_amount != null
    ? `≈ ${money(lang, c.converted_amount, c.converted_currency)} (${t("approx")})`
    : null;
  return [
    { label: t("field_merchant"), value: c.merchant || label(t, "type", c.transaction_type) },
    { label: t("field_amount"), value: amount, approx },
    { label: t("field_at"), value: dayTime(lang, c.at) },
    { label: t("field_city"), value: c.city },
    { label: t("field_channel"), value: label(t, "channel", c.channel) },
    { label: t("field_product"), value: `${label(t, "product", c.product_type)}${c.last4 ? ` •••• ${c.last4}` : ""}` },
    { label: t("field_status"), value: label(t, "tx", c.status) },
  ].filter((row) => row.value);
}

// Send is enabled only with something to send; an empty field never reaches the API.
export function canSend(text) {
  return String(text).trim().length > 0;
}

// The review time of a case with a person, as the chat gave it, with its label: it comes from
// the demo policy's queue.
export function reviewLine(t, item) {
  if (item.review_hours == null) return null;
  return { text: t("reviewTime", { hours: item.review_hours }), label: t("reviewLabel") };
}

// The second line of a movement: city and channel, and the type unless the channel already
// says it is a purchase ("Compra en línea").
export function movementDetail(t, m) {
  const type = m.transaction_type === "Purchase" ? null : label(t, "type", m.transaction_type);
  return [type, m.city, label(t, "channel", m.channel)].filter(Boolean).join(" · ");
}

// The color of a status: done (registered, approved), in review, or rejected.
export function statusTone(status) {
  if (status === "registered" || status === "approved" || status === "answered") return "ok";
  if (status === "rejected") return "bad";
  return "warn";
}

// Which conversation the chat is in. "Nueva conversación" starts another one; an answer that
// arrives for a conversation already left is dropped, so it cannot set its case back.
export function createConversation() {
  let turn = 0;
  return {
    current: () => turn,
    startNew: () => ++turn,
    isCurrent: (t) => t === turn,
  };
}

// The analyst's question on a clarification (TRZ-28): open with its deadline and a form, or
// already answered. The deadline is a date of the simulated clock; the time of the answer is
// not shown, since it runs on the real clock (design 10.2, rule 7).
export function infoRequestView(t, lang, item) {
  const r = item.info_request;
  if (!r) return null;
  if (r.status !== "open") return { question: r.question, answered: t("answered"), canAnswer: false };
  return {
    question: r.question,
    due: t(r.overdue ? "answerOverdue" : "answerBy", { date: day(lang, r.due_on) }),
    overdue: r.overdue,
    canAnswer: true,
  };
}

// How many clarifications wait for the customer's answer: the dot of the menu, until the
// notifications of TRZ-32.
export function openQuestions(items) {
  return items.filter((i) => i.info_request?.status === "open").length;
}

// What the customer screens say, built from API data. Pure functions, no DOM: the chat and the
// lists render their output, and tests/web checks it.

import { day, dayMonth, dayTime, label, money, number } from "./i18n.js";

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
  // An amount comes as a number and is written as every amount of this screen.
  if (chip.field === "amount" && chip.amount != null) {
    value = chip.currency ? money(lang, chip.amount, chip.currency) : number(lang, chip.amount);
  }
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
  // The network limit says how long is left of its window, in whole minutes rounded up, and
  // always the urgent way out: a customer who lost the card cannot wait for the window.
  if (error?.code === "ip_requests_limited") {
    const minutes = error.retryAfter ? Math.ceil(error.retryAfter / 60) : null;
    const wait = minutes === null
      ? t("error_ip_requests_limited")
      : minutes <= 1 ? t("error_ip_requests_limited_one") : t("error_ip_requests_limited_wait", { minutes });
    return `${wait} ${t("error_ip_requests_limited_urgent")}`;
  }
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
    { key: "merchant", label: t("field_merchant"), value: c.merchant || label(t, "type", c.transaction_type) },
    { key: "amount", label: t("field_amount"), value: amount, approx },
    { key: "at", label: t("field_at"), value: dayTime(lang, c.at) },
    { key: "city", label: t("field_city"), value: c.city },
    { key: "channel", label: t("field_channel"), value: label(t, "channel", c.channel) },
    { key: "product", label: t("field_product"), value: `${label(t, "product", c.product_type)}${c.last4 ? ` •••• ${c.last4}` : ""}` },
    { key: "status", label: t("field_status"), value: label(t, "tx", c.status) },
  ].filter((row) => row.value);
}

// The field is left empty when a message is sent (a failed one keeps its text in its bubble, with
// a retry) and when a session ends or starts: whoever signs in next must not see it.
export function draftOn(event, draft) {
  return ["send", "logout", "login"].includes(event) ? "" : draft;
}

// The greeting follows the language of the screen until the customer writes; after that, what is
// in the chat stays as it was written.
export function greetingFollowsLanguage(customerMessages) {
  return customerMessages === 0;
}

// The bubble of TRAZO while a reply is on its way: the word shown next to the animated dots, and
// the label read out by screen readers.
export function typingView(t) {
  return { word: t("typingWord"), label: t("typing"), dots: 3 };
}

// The mark of a message of the customer: none while it is sent or once it was, and "not sent"
// with a retry when the request failed (a limit, the network, the database).
export function outgoingNote(t, status) {
  return status === "failed" ? { text: t("notSent"), retry: t("retrySend") } : null;
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

// The opening of the chat (QA of TRZ-34). After a reload or in a duplicated tab the tab's case may
// already be with a person; the chat says so instead of greeting as if nothing were open.
const WITH_PERSON = new Set(["escalated", "pending_analyst_approval", "failed"]);

export function chatOpening(t, caseId, items) {
  const item = caseId
    ? (items || []).find((i) => i.source === "cases" && i.case_id === caseId)
    : null;
  if (!item || !WITH_PERSON.has(item.status)) return { text: t("chatIntro"), review: null };
  return { text: t("chatWithPerson", { caseId }), review: reviewLine(t, item) };
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
  if (status === "rejected" || status === "closed_no_info") return "bad";
  return "warn";
}

// Which conversation the chat is in. "Nueva conversación" starts another one; an answer that
// arrives for a conversation already left is dropped, so it cannot set its case back.
export function createConversation() {
  let turn = 0;
  // The turn whose request is in flight, or null. One request at a time: a message, an option or
  // a confirmation sent meanwhile would be lost or answered out of order.
  let flying = null;
  return {
    current: () => turn,
    startNew: () => {
      flying = null;
      return ++turn;
    },
    isCurrent: (t) => t === turn,
    busy: () => flying !== null,
    // Starts a request of the current conversation; null while another one is in flight.
    begin: () => {
      if (flying !== null) return null;
      flying = turn;
      return turn;
    },
    // Ends a request. True when its typed text may be cleared: it was answered, and its
    // conversation is still the current one.
    end: (t, ok) => {
      if (flying === t) flying = null;
      return Boolean(ok) && t === turn;
    },
  };
}

// The composer while a request may be in flight: Send (and Enter) only with text and nothing in
// flight, and the reply shown on its way meanwhile.
export function composerState(text, busy) {
  return { canSubmit: !busy && canSend(text), pending: Boolean(busy) };
}

// The analyst's question on a clarification (TRZ-28): open with its deadline and a form, or
// already answered. The deadline is a date of the simulated clock; the time of the answer is
// not shown, since it runs on the real clock (design 10.2, rule 7).
export function infoRequestView(t, lang, item) {
  return item.info_request ? requestView(t, lang, item, item.info_request, true) : null;
}

// Every question of the analyst on a clarification, oldest first, each with its answer or its
// form, as the analyst sees them in the dossier.
export function infoRequestViews(t, lang, item) {
  const list = item.info_requests?.length ? item.info_requests : item.info_request ? [item.info_request] : [];
  return list.map((r, i) => requestView(t, lang, item, r, i === list.length - 1));
}

// The marker the purge of old conversations writes (TRZ-41), in the database's Spanish; the
// screen shows it in its own language.
const PURGED = "[eliminado por retención]";
const shownText = (t, text) => (text === PURGED ? t("purgedText") : text);

function requestView(t, lang, item, r, last) {
  if (r.status !== "open") {
    // Only the latest answer is under review, and only while the case is not decided.
    const closed = item.status === "rejected" || item.status === "approved";
    return {
      question: shownText(t, r.question),
      answer: shownText(t, r.answer),
      answered: last && !closed ? t("answered") : null,
      canAnswer: false,
    };
  }
  return {
    question: shownText(t, r.question),
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

// Which deadline a clarification shows: the review time of a case with a person, the response
// deadline of a dispute or claim, or none. A case waiting for the customer's answer shows only
// the deadline to answer, in its question (TRZ-28); a rejected one is closed and shows none.
export function deadlineKind(item) {
  if (item.info_request?.status === "open") return "answer";
  if (item.status === "rejected" || item.status === "closed_no_info") return "closed";
  if (item.review_hours != null) return "review";
  if (item.due_date) return "due";
  return "none";
}

// What a closed clarification tells the customer: a rejection says it did not proceed, why in
// plain words from the closed list of reasons (TRZ-32 CA3), and what to do next. The analyst's
// note is never shown (QA of TRZ-27/28).
export function closedNote(t, item) {
  if (item.status === "closed_no_info") return t("noInfoNote");
  if (item.status !== "rejected") return null;
  const why = item.reason ? label(t, "reason", item.reason) : null;
  return why && why !== item.reason
    ? `${t("rejectedWhy", { reason: why })} ${t("rejectedNote")}`
    : t("rejectedNote");
}

// The status key of a clarification: a registered dispute whose audit an analyst reversed is in
// review by an analyst (TRZ-29 CA5), not "in review" as a bank claim says.
export function statusKey(item) {
  return item.source === "disputes" && item.status === "in_review" ? "under_review" : item.status;
}

// The counter on the avatar: notifications not read yet (TRZ-32 CA2).
export function unreadBadge(t, unread) {
  const n = Number(unread) || 0;
  return { hidden: n === 0, text: n > 9 ? "9+" : String(n), title: t("notificationsUnread", { n }) };
}

// ---- the trace of a case in the audit view of the demo (TRZ-34 CA4) ------------------------

// The switch of the audit view: off until the customer turns it on, in this tab only.
export const AUDIT_KEY = "trazo.customer.audit";

export function auditOn(stored) {
  return stored === "on";
}

export function auditSwitchView(t, on) {
  return { text: `${t("auditSwitch")}: ${t(on ? "auditSwitchOn" : "auditSwitchOff")}`, checked: on };
}

export function traceHref(caseId) {
  return `#/traza/${encodeURIComponent(caseId)}`;
}

// The case of a trace route, or null for any other route.
export function traceCase(hash) {
  const match = /^#\/traza\/(.+)$/.exec(hash || "");
  return match ? decodeURIComponent(match[1]) : null;
}

// The steps as the API tells them; the screen adds no figure, no date and no internal code.
export function traceLines(steps) {
  return (steps || []).map((s) => ({ text: s.text, traceId: s.trace_id }));
}

// Where a trace route goes. With the audit view off it goes back to Mis aclaraciones before any
// request; a case the API does not find for this customer goes back there too (QA CP-13). Any
// other failure stays on the screen with its message.
export function traceRoute(hash, visible, error) {
  const caseId = traceCase(hash);
  if (caseId === null) return null;
  if (!visible) return { redirect: "#/aclaraciones" };
  if (error) return error.status === 404 ? { redirect: "#/aclaraciones" } : null;
  return { caseId };
}

// The notifications in the language of the screen, as every other text (QA of TRZ-31/34/37).
export function notificationsPath(lang) {
  return `/me/notifications?lang=${encodeURIComponent(lang)}`;
}

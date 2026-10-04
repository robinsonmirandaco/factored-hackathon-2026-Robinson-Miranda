# Declarations

What this project declares about its evaluation, its data and its deployment: decisions taken before seeing results, changes made after the single run on the held-out split, findings that went against the design, and known limits. Each item names where it can be checked.

Labels used across the repository: **[offline]** measured on the held-out cases with a simulated client; **[simulado]** reviews, analysts or demo state are simulated; **[projection]** a cost or volume computed from assumptions. Nothing here was measured in production.

## 1. The held-out split and the single run

1.1. **One run per system on the frozen split.** The test split (376 cases from 94 base cases, each in ES-MX, ES-CO, ES-AR and PT-BR) was frozen with its sha256 hashes before any tuning run. TRAZO ran it 3 times and the free agent once, all at commit `a952a6b`. Source: `docs/reports/evaluacion.md` (Run, Cases); `eval/runs.jsonl`; `eval/splits/manifest.json`.

1.2. **"Opened once" means no tuning on it, not one file read.** `docs/reports/evaluacion.md` (Declarations) says the split was "opened once for this evaluation". What that sentence holds: no system ran on the test split before the single run, and no weight, threshold, prompt or rule was chosen on it. The files were read more than once:
- every recorded run on the test split reads each file once to load it (11 runs in `eval/runs.jsonl`: 3 of TRAZO, 1 of the free agent, 3 threshold variants, 3 PT-BR degradation runs, 1 with the fact checker off);
- the comprehension reports (Haiku from the harness cache, Sonnet once), the identification report and its reliability diagram, the analysis report and the ablations each loaded the labels;
- after the session of 2026-10-03 the labels were read again, with no system run, to regenerate `make eval` twice, to break down the results and to compute the latency of Haiku from the cache.

1.3. **Accesses to the test files outside the runs.** Three, recorded as found:
- 2026-09-30 16:55: the access time of both test files changed at the end of a full test run. An audit hook on every file open, repeated over the full suite and two report commands, saw no open of the real files; the hashes matched the manifest. Probable cause, not proven: an indexing or backup process of the operating system.
- 2026-10-01 11:21: one deliberate run of `make cases OFFLINE=1` to list the customers of every case for the demo seed (commit `f9ff198`). An audit hook recorded the test files opened in write mode only (rewritten with the same bytes) and none read; 0 LLM calls; the four hashes unchanged.
- 2026-10-01 11:51 and 11:52: last access times before the run, cause not proven. No work of TRZ-43 to TRZ-45 opened the files; the harness refuses the test split outside `make eval-run`.

## 2. After the single run

2.1. **Changes made after the run were measured on development only.** Three causes of failure on the held-out split got a change afterwards: a security event raised from the text of the message (`df22469`), an injection stop decided by the analyst like any other case (`b66cfa1`), and the card blocked only after its dispute reads back (`2ef62a0`). On the development split, before against after: injection 0/20 to 20/20, other customer 0/16 to 16/16, tool failure 8/12 to 12/12, missed escalations 22.5% to 0.0%. The held-out split was not run again, so **the system as deployed has changes not measured on held-out cases**, and development cases are the ones it was built on. Source: `docs/reports/evaluacion.md` (After the single run: development only); `docs/reports/analisis.md` (What changed after the single run).

2.2. **Not changed after the run:** the three ES-MX messages read as out of scope and the misread card possession. Source: `docs/reports/analisis.md` (What changed after the single run).

2.3. **Analysis written after reading the results.** The error rules of TRZ-49 were written after the failures had been read; they sort the failures and change no measure. Source: `docs/reports/analisis.md` (header, Error analysis).

2.4. **Ablations run after the results were read.** TF-IDF + logistic regression, the trained ranker and the fact checker off were evaluated once on the held-out split on 2026-10-03, with every setting committed before; nothing was adjusted after. Source: `docs/reports/analisis.md` (Ablations).

2.5. **Additions with no new call.** After the run, computed from the stored runs: the table of unsafe outcomes with and without unsupported claims, the note on the tool-failure cases, a format change of the line of file opens, and the reliability diagram of identification. No measure changed. Source: `docs/reports/evaluacion.md` (Declarations); `docs/reports/identificacion.md` (Reliability diagram).

## 3. Labels and ground truth

3.1. **Injection labels kept as `security_blocked`.** Decided before the run, although TRAZO had no injection detector at that commit (golden case 22). An injection succeeds when the system acts beyond the customer's legitimate charge (another charge, card or customer); acting on that charge alone counts as `should_have_escalated`. Result on the held-out split: 15 injection failures, 0 successful injections for TRAZO, 1 for the free agent. Source: `docs/reports/evaluacion.md` (Declarations, Unsafe outcomes); `docs/reports/analisis.md` (Failures by stage and cause).

3.2. **The reviewers registered the injection cases.** In 4 handwritten injection cases both human reviews registered the customer's own charge, as TRAZO did, instead of stopping them. The label is debatable; it was kept, and TRAZO is counted against it. Source: `docs/reports/analisis.md` (Handwritten cases where the reviews pick another action).

3.3. **Provenance of the handwritten labels.** Review 1 was done from a blank sheet on 2026-09-30. Before review 2 the author had read a version prefilled by a model. Review 2 was done from a blank sheet on 2026-10-02, 45.6 hours after review 1. Agreement between reviews: 1.000 on the eight fields, kappa 1.000 on intent and action. Against the constructed labels, action agrees on 48 of 60; the 12 differences are analyzed. Source: `docs/reports/evaluacion.md` (Cases); `eval/splits/manifest.json` (`handwritten_review`).

3.4. **Other-customer cases receive only the text.** Neither system is sent the foreign `customer_id` field in the request body, the only trigger of TRAZO's security event at the run's commit. Source: `docs/reports/evaluacion.md` (Declarations).

3.5. **Tool-failure cases stay unsafe.** In 6 of 12 tool-failure cases the registration did not verify, the case went to a person, and the card block the customer had confirmed was written. They stay counted as `should_have_escalated`. Fixed after the run (2.1). Source: `docs/reports/evaluacion.md` (By case type).

3.6. **Ambiguous channel labels.** Some cases are labeled with the channel the message mentions where the customer only says where they saw the charge ("lo vi en la app" labeled App), and some ATM purchases speak of a store's cashier. Part of the channel accuracy of both readers comes from these labels; they were not changed. Source: `docs/reports/comprension_prueba.md` (channel column).

3.7. **The order of the escalation rules is not exercised by the labels.** No development base case meets two escalation conditions at once, so the order inside the escalation rules is covered by unit tests, not by the label agreement (137 of 137 on development). Source: `docs/reports/politica.md` (Agreement); `tests/unit/test_policy.py`.

## 4. The free agent baseline

4.1. **Same policy, same tools, two steps from TRAZO's code.** The free agent prompt carries the policy verbatim and two steps that come from TRAZO's code, not from the policy file: how a charge is searched (options or one more detail) and showing the charge before disputing it. Source: `docs/reports/evaluacion.md` (Declarations); `config/prompts/free_agent.yaml`.

4.2. **Tuned as much as TRAZO.** Its prompt had one iteration on the development split (`free-agent-3`). When it asks a question in plain text, the simulated client answers with every clue it remembers, once, which favors it. Source: `docs/reports/evaluacion.md` (Declarations).

4.3. **Checked in observer mode.** The free agent has no verifier; its replies go through TRAZO's fact checker only to count claims without a source, against the facts its own tools returned. Source: `docs/reports/evaluacion.md` (Declarations).

4.4. **One run only.** Two more would cost about 6.5 USD, so its variability is not measured. Source: `docs/reports/analisis.md` (Variability over repetitions).

4.5. **Its observer counts predate a change of the checker.** Since `4c855a3` the fact checker reads "mil", "lucas" and "millones" as part of an amount. 29 replies of the free agent in 28 held-out cases have such a figure, so its counts of unsupported claims could change if it were scored again; it was not. No reply TRAZO sent in the held-out runs has one. Source: stored run files; `src/app/domain/fact_check.py`.

## 5. Measures whose definition changed during the project

5.1. Handoffs to a person (escalations and approvals) stopped counting as `llm_fallback` at `b0b3f40`, and replies written by code stopped recording zero tokens and cost at `e3815a8`. Source: `docs/reports/evaluacion.md` (Declarations).

5.2. The registration receipt is written by code since `509bdac`: fewer replies are written by the LLM, tokens and cost per case drop, and fewer replies go through the fact checker. Runs before that commit are not comparable on those measures. Source: `docs/reports/evaluacion.md` (Declarations).

5.3. The harness runs every case in a new schema, so the audit sample starts at draw 1, which seed 20260934 selects: the first registration of each case goes to the audit sample. The audit rate of the harness is not ρ = 0.10; nothing changes for the customer or the score. Source: `docs/reports/evaluacion.md` (Declarations).

5.4. **What the LLM writes and what the code writes.** Every handoff to a person (escalation or approval, with the reason of its rule and a review time labeled [simulado]), the confirmation question, the registration receipt and every security, failure and redirection reply are written by code. The LLM writes only informative turns, the presentation of options, the closing of a recognized charge and the explanation of a pending duplicate, and each of those goes through the fact checker. Source: `src/app/services/agent.py` (`CODE_WRITTEN_OUTCOMES`); `b0b3f40`, `509bdac`.

## 6. Comprehension (the learned component)

6.1. **Model choice rule fixed before results.** Haiku 4.5 stays unless Sonnet beats the mean of its 3 runs by at least 2 points of intent F1 or amount accuracy, without worse faithful fragments, with p95 under 5 s and at most twice the cost per case. Sonnet compared is `claude-sonnet-5-5`, not `claude-sonnet-5` as in the design; it does not accept `temperature` and ran with default sampling, 1 run, a 15 s deadline. Haiku was kept. Source: `docs/adr/0004-llm-comprehension-as-the-learned-component.md` (Model choice); `docs/reports/comprension_prueba_sonnet.md`.

6.2. **Haiku latency on the test split shows 0.** Its readings came from the harness cache, paid by TRAZO's runs. The latency first paid for those 1,128 readings: p50 1,465 ms, p95 2,427 ms. Source: `docs/reports/evaluacion.md` (Declarations).

6.3. **Where the rules tie or win.** On the held-out split, out-of-scope recall is 100.0% for rules and for Haiku; faithful fragments are 100.0% for rules against 99.9% for Haiku. Source: `docs/reports/analisis.md` (Comprehension: four systems).

6.4. **Three in-scope messages redirected.** All three ES-MX (`test-8f0468319d`, `test-92deaff50f`, `test-c92130c4e3`): comprehension read them as out of scope and TRAZO answered with the fixed redirection, while their other three variants were registered. They are the 3 `dissuaded_dispute` outcomes of TRAZO. Source: `docs/reports/analisis.md` (Discrepancies of trazo); `docs/reports/evaluacion.md` (Unsafe outcomes).

6.5. **Variant detection is weak.** The language variant read by the LLM matches the message in 62.5% of cases; the autonomy cell uses the language (es, pt), not the variant. Source: `docs/reports/comprension_prueba.md` (Overall).

6.6. **Prompt leak check.** On the test split, the comprehension harness checks that no example of the prompt comes from outside the development split and that the prompt holds no id, base id or message of a calibration or test case (`check_prompt_sources`, `155a220`). Source: `pipeline/comprehension_llm.py`.

6.7. **Rules baseline not adjusted.** The rules read "Como faço para investir no CDB?" as an unrecognized charge, not out of scope; the rule was not changed, because it would change the baseline compared. Source: `src/app/domain/comprehension_rules.py`.

6.8. **Language detector, tuned on development, measured on the held-out split after the run.** Its word lists were set on the development split (`docs/reports/idioma.md`, 599 of 600 there). On the test split, read for the first time in the analysis after the single run, it gets the language right in 367 of 376 messages (97.6%); handwritten 57 of 60, code-mixed portuñol 25 of 28, PT-BR 87 of 94. Source: `docs/reports/analisis.md` (Components on the held-out split).

## 7. Identification

7.1. **Empty conformal set.** Decided on 2026-09-30 (`8cbf75f`), before the run: an empty set with no rejection and more than one candidate asks for a detail; empty by rejection or with no candidate stays "not found". Parameters unchanged. Source: `docs/reports/evaluacion.md` (Declarations).

7.2. **Amount tolerance, prompted by a test case.** The inconsistency between the noise model and the old tolerance (0.35) was noticed while reviewing one test case during the preparation of the curated cases, before any run on the test split. The new tolerance (ln 2 for an approximate amount) comes from the declared noise model, and the refit used only development and calibration (TRZ-55, `6680eb2`). Because the change was prompted by a test case, the coverage measured on the test split may be slightly biased in favor of the system. Source: `docs/reports/identificacion.md` (Amount tolerance); `docs/reports/evaluacion.md` (Declarations).

7.3. **Hedge words lost in paraphrase.** The paraphrase of generator A sometimes drops the hedge word of an approximate amount, so neither rules nor the LLM read those messages as approximate and they are scored with the exact tolerance. Seen on development and calibration while preparing TRZ-55; not counted in a report. Source: `pipeline/cases/render.py` (paraphrase).

7.4. **Coverage below 95% in two groups.** On calibration (in-sample for q-hat), segment Plus and category missing_data were below 95%. On the test split with the LLM: Plus 76/80 (95.0%; 87.8% to 98.0%), missing_data 16/20 (80.0%; 58.4% to 91.9%). Source: `docs/reports/identificacion.md` (By segment, By category); `docs/reports/analisis.md` (Components on the held-out split).

7.5. **Soft date search, by decision.** A date outside the understood window costs points but does not remove a candidate, because customers misremember dates and the conformal set covers that uncertainty. Source: `src/app/domain/identification.py`.

## 8. Autonomy watch (Wilson) [simulado]

8.1. **The desk check of design 6.7 was wrong.** The design expected 100% detection at a 40% true error. Simulated with seed 20261003, 10,000 streams: one block of 20 reviews detects it in 25.25% of streams (exact 24.47%); within 10 blocks, 93.76%; median 60 reviews to detect. At a 10% true error, 1 false demotion in 10,000 streams within 10 blocks (criterion fixed before: at most 5%), so N and the thresholds were not changed. Source: `docs/reports/evaluacion.md` (Known error rates).

8.2. **The degradation scenario did not degrade (TRZ-47 CA2, declared as a finding, no further run).** Removing the one Portuguese example from the comprehension prompt left PT-BR safe resolution at 67.0%, as the base, and actions an analyst would reverse went from 7 to 3. No cell was demoted, so cases to detect and unsafe outcomes avoided are not defined. Cost 0.3964 USD. Source: `docs/reports/evaluacion.md` (Degradation of PT-BR comprehension).

8.3. **Wilson does not stop the error rate the system has.** In the base run, unrecognized charge has about 91 (ES) and 96 (PT) unsafe outcomes per 1,000 cases of the cell, in the resampled streams, that the watch does not stop: demotion needs a reversal rate near 50% in a block, and the reviews of handovers, almost always right, dilute the audits. Counting only the audits raises the expected reversal rate to about 15%, still far below. Source: `docs/reports/evaluacion.md` (Degradation); `docs/reports/analisis.md` (Autonomy watch diluted by the reviews of handovers).

8.4. **Reference values reproduced.** With z = 1.645, 10 of 20 gives W ≈ 0.327 and 9 of 20 gives W ≈ 0.284. Source: `tests/unit/test_wilson.py`.

8.5. **Thresholds of the policy.** 500 and 1,000 USD and α = 0.05 are the values of the design; the sensitivity table does not change them. Halving the amounts drops safe resolution from 66.2% to 32.4%. Source: `docs/reports/evaluacion.md` (Sensitivity of the thresholds); `config/policy.yaml`.

## 9. Deployment and operation

9.1. **Fixed simulated clock.** The business date of the deployment is fixed (`TRAZO_NOW=2026-06-17T23:59:00`) so the dataset's 120-day window holds charges and the demo is reproducible. Anything that depends on time passing gets the date from outside: the closing of a request for information unanswered past its 5 business days does not happen by itself in Railway; it is shown with the command and the tests (`025d282`). Source: `.env.example`; `src/app/domain/clock.py`; `tests/integration/test_info_request_cycle.py`.

9.2. **Retention runs on the real clock.** Conversations older than 90 days lose their text (customer message, reply, translation, analyst question and note, quoted fragments), replaced by a retention marker; the audit rows and decisions stay. Retention is a real-world obligation, so it uses the real clock; during the evaluation nothing in the deployment is old enough to be purged. Source: `a2f6212`, `184cd06`; `CONVERSATION_RETENTION_DAYS` in `.env.example`.

9.3. **Email is off by default, and off in production during the evaluation.** With the flag off nothing is written or sent. With it on, mail goes through an outbox, only to a test inbox that is also on an allow list; the database keeps no customer email. Source: `EMAIL_ENABLED` in `.env.example`; `97e4c96`.

9.4. **Capacity measured locally, not in Railway.** One API process and Postgres in Docker on one machine, criterion fixed before (p95 of `/chat` at most 1.5 times that of one case, under 1% errors), never against the public URL:
- rules only: at least 64 simultaneous cases;
- LLM simulated with Haiku's measured latency: **32 simultaneous cases** (p95 3,125 ms, 1.21 times one case); 64 does not hold (2.24 times). Measured again after the change of the LLM deadline (`d40f042`), with the same result as before it (32; 2.09 times at 64);
- real Haiku: 8 simultaneous cases reached within the budget, p95 1,800 to 2,247 ms, no errors; not a capacity. At the use per case of that run, the key's output-token limit allows about 800 cases per minute.
No level crossed a limit of the machine. Source: `docs/reports/carga.md`.

9.5. **Where the load limit comes from: a reading, not a direct measurement.** What was measured: at 64 simultaneous cases with the simulated LLM no LLM call falls back to the rules, before or after the change of the LLM deadline, and the p95 of `/chat` is 3,236 ms at the server against 5,773 ms at the client, so about 2.5 s pass before a request reaches the route. The reading: the limit is the 40 threads that serve the synchronous routes, not the LLM. A first reading named the 8-thread LLM pool; the pool is now configurable (`LLM_POOL_SIZE`, 8 by default) and the 5 s deadline starts when a thread runs the call (`4ed9430`), which did not move the capacity, so that reading was wrong. Only the pool at 8 was measured: a run at 16 was not made, because the load showed the bottleneck is not in that pool. The two runs after the change are recorded with commit `be2c77168`, renamed before publishing when one commit message was corrected; its tree is that of `dd04576`. Source: `docs/reports/carga.md` (Scenario `simulated`); `src/app/adapters/llm.py`.

9.6. **The role check found the cron running as superuser.** The API refuses to start with a privileged database role; the scheduled jobs did not check it. After adding the same check (`a87654a`), the Railway cron refused to run: it had been connecting as the database superuser since it was created. Its URL now uses the application role. Source: `a87654a`.

9.7. **Login limit per address.** 30 requests per address and endpoint every 15 minutes, address taken from `X-Real-IP`, which Railway's edge sets; `X-Forwarded-For` is never read. Each of `/auth/otp/request`, `/auth/otp/verify` and `/auth/analyst/login` has its own count, so requests to one do not spend the limit of another. Checked on the public URL on 2026-10-02: the 31st request to `/auth/otp/request` got 429 with a forged `X-Forwarded-For` and with a forged `X-Real-IP`. Source: `20ce74a`; `src/app/api/routes.py`; `CLIENT_IP_HEADER` in `.env.example`.

9.8. **LLM spend of the deployment.** `/chat` counts customer turns per client address, with the mechanism of the login limit and a count of its own: `CHAT_IP_REQUEST_LIMIT`, 120 every 15 minutes by default (`9d61ef8`). **During the evaluation the public URL runs it at 300**, because several judges may share one network; they also share the count. Past the limit the answer is 429 with `Retry-After`; the screen says how many minutes are left and always gives the urgent way out: block the card with the bank's app or its block line (`b6768ce`, `910762f`). The API key of Railway has its own spend cap. It expires on 2026-10-26; after that date the deployed demo falls back to the rules, as in an LLM outage (design 11.5). Source: `src/app/services/auth.py`; `src/app/api/routes.py`; `web/assets/view.js`; deployment configuration.

9.9. **Shared demo personas.** Every evaluator uses the same demo customers, and a charge can have one open dispute, so two evaluators disputing the same charge collide. "Reset demo [simulado]" in the analyst console restores the starting state. Source: `config/demo.yaml`; `076036d`.

9.10. **Walkthrough of design 10.3.** Step 3 ("asks to talk to a person") has no intent; the demo uses a 500 to 1,000 USD charge that goes to analyst approval instead. Step 4 depends on the LLM and cannot be rehearsed with the rules. Source: `config/demo.yaml`.

9.11. **Demo access codes are not in the repository.** The challenge statement says: "Do not include private customer records, credentials, or restricted data in public submissions." The README gives the public URL and the invented documents of the demo customers; the one-time demo code and the analyst's test password are sent in the submission email, not published. Real secrets (API keys, the document hash key, database passwords) live only in environment variables. Source: `README.md` (Try the public demo); `.env.example`.

9.12. **The time of an audit row, and the trace by turn.** Since `3b7517b` each audit row records the time the application writes it, not the start of its transaction, so the steps of one turn keep their own instants; rows written before share one instant per transaction. Nothing measured reads this time: latency comes from `latency_ms`, the order from the row id, and the retention purge still cuts at 90 days of the real clock (`tests/integration/test_retention.py`); the append-only trigger still refuses any update or delete, the time included. The customer's trace in the audit view of the demo and the analyst's history group the steps by turn, number them in audit order and give only time differences inside a turn, never a date or a clock time of the real clock (design 10.2, rule 7); a turn written before shows no times. A message turn starts with the customer's message as stored, redacted: with the answer to an analyst's question, the second free text a history line tells (`184bb9d`). The structure was added after the single run and changes no measure. Source: `src/app/adapters/db/audit.py`; `src/app/domain/history.py`.

## 10. Known limits of the product

10.1. **PII redaction** does not catch an address written in the message; a customer first name that is also a word or a merchant (Luz, Paz, "Tienda Ana") is replaced by `[NAME]` too, losing that text. Any bare run of exactly 10 digits is redacted as a phone (`eb1d0b5`), so an amount written as 10 digits with no separator would lose its clue; no development or calibration message has one. Source: `src/app/domain/pii.py`.

10.2. **The fact checker** does not read numbers in words ("quince días"); "mil", "lucas" and "millones" are read as part of an amount since `4c855a3`. It does not detect an invented merchant missing from the customer's list; its lexicon of forbidden requests is conservative ("we will never ask for your password" is blocked too); it may block a merchant the LLM repeats from the customer's own words. Source: `src/app/domain/fact_check.py`.

10.3. **Replies still written by the LLM.** The closing of a recognized charge and the explanation of a pending duplicate are written by the LLM and checked by the fact checker, which does not catch every new wording of a contact promise; these replies can promise something the system does not do. Source: `docs/reports/evaluacion.md` (Declarations).

10.4. **The analyst's question is not translated.** It is free text in Spanish, shown as written; a Portuguese-speaking customer reads it in Spanish. The console warns the analyst. Source: `web/assets/analyst.js`.

10.5. **Synthetic fixture.** Colombian customers of the CI fixture have COP amounts on a USD scale and no exchange rate, so none can complete a registration in that stack; the full Portuguese registration is shown with the demo personas on the cohort. Source: `src/app/adapters/ingest/synthetic.py`.

10.6. **Design limits that stand.** Synthetic data with template texts; constructed ground truth with assumed misunderstanding scenarios; all Portuguese generated; a marginal conformal guarantee under exchangeability, calibrated on generated cases; simulated analysts and Wilson parameters chosen, not calibrated on real traffic; one replica; a test identity service with no real provider; a demonstration policy. Source: design section 16, summarized in the README.

10.7. **Cosmetic limits of the LLM's wording.** Its text can format an amount differently from the card on screen ("13,2 USD" against "USD 13.20"), and it greets with "Hola" on every turn. Neither changes a fact (the fact checker reads the amount); both were left as they are, because changing the composing prompt after the single run would change the system that was measured. Source: `src/app/adapters/llm.py` (`COMPOSE_SYSTEM`).

10.8. **A case stopped for security keeps the greeting on reload.** The chat replaces its greeting when it opens on a case with a person (`ffb04f3`), but a case stopped for security is not listed in Mis aclaraciones, so the web cannot tell and greets as usual. Source: `web/assets/view.js` (`chatOpening`).

10.9. **A draft is lost after an expired session.** The chat field is cleared on sign out and on every sign in, so the next person on the browser never sees what the previous one typed (`cd5ca92`). Someone whose session expires with a message typed loses that text when signing in again: the screen cannot tell the same person, since `/me` carries no customer id. A message whose send failed keeps its text in its bubble, with a retry (`5d71ca1`). Source: `web/assets/customer.js`; `web/assets/view.js`.

## 11. Not built (future work)

- Confirm the dispute and the card block separately on the lost-card route (today one confirmation runs both, in order).
- Give the comprehension prompt the pending question of the case, so short answers ("fueron 900") are not read as out of scope; today the code compensates.
- Separate tolerances for "about" and "more than" amounts.
- An intent to ask for a person.
Each would go through the gate of learned components (held-out, baseline, 3 runs). Source: `docs/reports/analisis.md` (The five main causes) and this file.

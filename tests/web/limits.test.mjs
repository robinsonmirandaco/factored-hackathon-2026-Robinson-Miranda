// How the network limit says how long to wait (QA of the hardening, TRZ-40).
import assert from "node:assert/strict";
import { test } from "node:test";

import { createClient } from "../../web/assets/api.js";
import { translator } from "../../web/assets/i18n.js";
import { errorText } from "../../web/assets/view.js";

const es = translator("es");
const pt = translator("pt");
const URGENT_ES =
  "Si perdiste tu tarjeta o te la robaron, bloquéala de inmediato con la opción de bloqueo de la app de tu banco o llamando a la línea de bloqueo que aparece en el sitio oficial del banco.";
const URGENT_PT =
  "Se você perdeu o cartão ou ele foi roubado, bloqueie-o agora mesmo pela opção de bloqueio do app do seu banco ou ligando para a central de bloqueio indicada no site oficial do banco.";

test("the network limit says how many minutes are left, rounded up, and the urgent way out", () => {
  const limited = (seconds) => ({ code: "ip_requests_limited", retryAfter: seconds });
  assert.equal(
    errorText(es, limited(700)),
    `Hubo demasiadas solicitudes desde tu red. Inténtalo de nuevo en unos 12 minutos. ${URGENT_ES}`,
  );
  assert.equal(
    errorText(pt, limited(700)),
    `Houve solicitações demais a partir da sua rede. Tente de novo em cerca de 12 minutos. ${URGENT_PT}`,
  );
  assert.equal(
    errorText(es, limited(30)),
    `Hubo demasiadas solicitudes desde tu red. Inténtalo de nuevo en un minuto. ${URGENT_ES}`,
  );
  assert.equal(
    errorText(pt, limited(30)),
    `Houve solicitações demais a partir da sua rede. Tente de novo em um minuto. ${URGENT_PT}`,
  );
  // Without the header the text stays general.
  assert.equal(
    errorText(es, { code: "ip_requests_limited" }),
    `Hubo demasiadas solicitudes desde tu red. Espera unos minutos e inténtalo de nuevo. ${URGENT_ES}`,
  );
});

test("the API client reads Retry-After into the error", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ error_code: "ip_requests_limited", message: "x", trace_id: "t" }), {
      status: 429,
      headers: { "content-type": "application/json", "retry-after": "700" },
    });
  try {
    await assert.rejects(createClient("customer").call("/chat", { method: "POST", body: {} }), (e) => {
      assert.equal(e.status, 429);
      assert.equal(e.code, "ip_requests_limited");
      assert.equal(e.retryAfter, 700);
      return true;
    });
  } finally {
    globalThis.fetch = original;
  }
});

// How the network limit says how long to wait (QA of the hardening, TRZ-40).
import assert from "node:assert/strict";
import { test } from "node:test";

import { createClient } from "../../web/assets/api.js";
import { translator } from "../../web/assets/i18n.js";
import { errorText } from "../../web/assets/view.js";

const es = translator("es");
const pt = translator("pt");

test("the network limit says how many minutes are left, rounded up", () => {
  const limited = (seconds) => ({ code: "ip_requests_limited", retryAfter: seconds });
  assert.equal(
    errorText(es, limited(700)),
    "Hubo demasiadas solicitudes desde tu red. Inténtalo de nuevo en unos 12 minutos.",
  );
  assert.equal(
    errorText(pt, limited(700)),
    "Houve solicitações demais a partir da sua rede. Tente de novo em cerca de 12 minutos.",
  );
  assert.equal(
    errorText(es, limited(30)),
    "Hubo demasiadas solicitudes desde tu red. Inténtalo de nuevo en un minuto.",
  );
  assert.equal(
    errorText(pt, limited(30)),
    "Houve solicitações demais a partir da sua rede. Tente de novo em um minuto.",
  );
  // Without the header the text stays general.
  assert.equal(
    errorText(es, { code: "ip_requests_limited" }),
    "Hubo demasiadas solicitudes desde tu red. Espera unos minutos e inténtalo de nuevo.",
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

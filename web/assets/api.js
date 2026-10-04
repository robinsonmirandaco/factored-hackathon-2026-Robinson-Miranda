// Calls to the TRAZO API from the same origin. The session token lives in sessionStorage: it
// survives a reload, dies with the tab, and each tab holds its own session, so a customer and
// an analyst can be open side by side. It is never put in a URL or in localStorage.

export class ApiError extends Error {
  constructor(status, code, message, traceId, retryAfter = null) {
    super(message || code || `HTTP ${status}`);
    this.status = status;
    this.code = code;
    this.traceId = traceId;
    // Seconds to wait, from Retry-After, when the server says so (a 429 of the network limit).
    this.retryAfter = retryAfter;
  }
}

function read(key) {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key, value) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    // Storage blocked: the session lasts only while the page stays open.
  }
}

export function createClient(role) {
  const tokenKey = `trazo.${role}.token`;
  let memory = read(tokenKey);

  async function call(path, { method = "GET", body, headers: extra = {} } = {}) {
    const headers = { accept: "application/json", ...extra };
    if (body !== undefined) headers["content-type"] = "application/json";
    if (memory) headers.authorization = `Bearer ${memory}`;
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "omit",
    });
    const data = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      const wait = Number(response.headers.get("retry-after"));
      const error = new ApiError(
        response.status, data?.error_code, data?.message, data?.trace_id,
        Number.isFinite(wait) && wait > 0 ? wait : null,
      );
      if (response.status === 401 && memory) {
        window.dispatchEvent(new CustomEvent("trazo:session-lost", { detail: error }));
      }
      throw error;
    }
    return data;
  }

  return {
    call,
    hasSession: () => Boolean(memory),
    setToken(token) {
      memory = token;
      write(tokenKey, token);
    },
    clear() {
      memory = null;
      write(tokenKey, null);
    },
    store: { read, write },
  };
}

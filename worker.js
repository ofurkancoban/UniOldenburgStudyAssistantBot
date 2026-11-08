/**
 * Cloudflare Worker Reverse Proxy for Uni Oldenburg Services (Stud.IP & StuMS)
 *
 * Request:  https://<worker>/<target_host>/<path>?<query>
 *           X-Proxy-Secret: <PROXY_SECRET>        (required)
 *           Cookie: <cookies for the target host> (sent by the bot as-is)
 *
 * Response: the upstream response, unmodified, except that every Set-Cookie
 *           header is moved to X-Upstream-Set-Cookie. The bot stores those
 *           cookies under the real university host (Domain/Path intact), so
 *           cookies of different hosts (e.g. the StuMS and NetIQ JSESSIONID)
 *           never collide under the worker's hostname. Redirects are not
 *           followed here; the bot follows them hop by hop.
 */

const ALLOWED_TARGET_HOSTS = new Set([
  "studip.uni-oldenburg.de",
  "elearning.uni-oldenburg.de",
  "stums.uni-oldenburg.de",
  "idp.uni-oldenburg.de",
  "login.uni-oldenburg.de",
  "auth.uni-oldenburg.de",
]);

// Headers that must not leak to the university or that Cloudflare sets itself.
const STRIP_REQUEST_HEADERS = [
  "x-proxy-secret",
  "x-target-host",
  "host",
  "cf-connecting-ip",
  "cf-ipcountry",
  "cf-ray",
  "cf-visitor",
  "x-forwarded-for",
  "x-forwarded-proto",
  "x-real-ip",
];

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/healthz") {
      return new Response(JSON.stringify({ status: "ok", service: "studip-cf-worker-proxy", version: 3 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    // A proxy without a secret would be an open proxy into the university.
    const configuredSecret = env.PROXY_SECRET || "";
    const secret = request.headers.get("X-Proxy-Secret") || "";
    if (!configuredSecret || !timingSafeEqual(secret, configuredSecret)) {
      return new Response("Unauthorized", { status: 401, headers: { "X-Proxy-Error": "auth" } });
    }

    // Pattern: /<target_host>/<path>
    const pathSegments = url.pathname.split("/");
    const targetHost = (pathSegments[1] || "").toLowerCase();
    if (!ALLOWED_TARGET_HOSTS.has(targetHost)) {
      return new Response(`Forbidden: host '${targetHost}' is not allowed`, {
        status: 403,
        headers: { "X-Proxy-Error": "host" },
      });
    }
    const targetPath = "/" + pathSegments.slice(2).join("/");
    const targetUrl = `https://${targetHost}${targetPath}${url.search}`;

    const upstreamHeaders = new Headers(request.headers);
    for (const h of STRIP_REQUEST_HEADERS) {
      upstreamHeaders.delete(h);
    }

    let response;
    try {
      response = await fetch(targetUrl, {
        method: request.method,
        headers: upstreamHeaders,
        body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
        redirect: "manual",
      });
    } catch (err) {
      return new Response(`Proxy Error: ${err.message}`, { status: 502, headers: { "X-Proxy-Error": "upstream" } });
    }

    const respHeaders = new Headers(response.headers);
    const rawCookies = typeof response.headers.getSetCookie === "function" ? response.headers.getSetCookie() : [];
    respHeaders.delete("Set-Cookie");
    for (const c of rawCookies) {
      respHeaders.append("X-Upstream-Set-Cookie", c);
    }
    // Marks a genuine university response, so the bot can tell it apart from
    // an error page of the Cloudflare platform itself (limits, crashes).
    respHeaders.set("X-Proxy-Upstream", "1");

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: respHeaders,
    });
  },
};

/**
 * Cloudflare Worker Reverse Proxy for Uni Oldenburg Services (Stud.IP & StuMS)
 *
 * Routes bot requests through Cloudflare's distributed edge network.
 * Preserves NetIQ SSO / Stud.IP cookies, OAuth bearer headers, and redirects.
 */

const ALLOWED_TARGET_HOSTS = new Set([
  "studip.uni-oldenburg.de",
  "elearning.uni-oldenburg.de",
  "stums.uni-oldenburg.de",
  "idp.uni-oldenburg.de",
  "login.uni-oldenburg.de",
]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 1. Health check endpoint
    if (url.pathname === "/healthz") {
      return new Response(JSON.stringify({ status: "ok", service: "studip-cf-worker-proxy" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    // 2. Secret authentication check
    const secret = request.headers.get("X-Proxy-Secret") || url.searchParams.get("proxy_secret");
    const configuredSecret = env.PROXY_SECRET || "";
    if (configuredSecret && secret !== configuredSecret) {
      return new Response("Unauthorized", { status: 401 });
    }

    // 3. Extract target host and subpath
    // Pattern: /<target_host>/<path> (e.g. /elearning.uni-oldenburg.de/dispatch.php/...)
    let targetHost = "";
    let targetPath = url.pathname;

    const pathSegments = url.pathname.split("/").filter(Boolean);
    if (pathSegments.length > 0 && ALLOWED_TARGET_HOSTS.has(pathSegments[0].toLowerCase())) {
      targetHost = pathSegments[0].toLowerCase();
      targetPath = "/" + pathSegments.slice(1).join("/");
    } else if (request.headers.get("X-Target-Host")) {
      targetHost = request.headers.get("X-Target-Host").toLowerCase();
    } else {
      targetHost = "elearning.uni-oldenburg.de";
    }

    if (!ALLOWED_TARGET_HOSTS.has(targetHost)) {
      return new Response(`Forbidden: host '${targetHost}' is not allowed`, { status: 403 });
    }

    // 4. Construct upstream target URL
    const targetUrl = new URL(targetPath + url.search, `https://${targetHost}`);

    // 5. Clone and adjust request headers
    const newHeaders = new Headers(request.headers);
    newHeaders.delete("X-Proxy-Secret");
    newHeaders.delete("X-Target-Host");
    newHeaders.set("Host", targetHost);

    // 6. Forward request to university server
    try {
      const response = await fetch(targetUrl.toString(), {
        method: request.method,
        headers: newHeaders,
        body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
        redirect: "manual",
      });

      const respHeaders = new Headers(response.headers);

      // 7. Rewrite Location header to keep redirects routed through the worker
      const loc = respHeaders.get("Location");
      if (loc) {
        try {
          const locUrl = new URL(loc, targetUrl);
          if (ALLOWED_TARGET_HOSTS.has(locUrl.hostname.toLowerCase())) {
            const rewrittenLoc = `https://${url.host}/${locUrl.hostname.toLowerCase()}${locUrl.pathname}${locUrl.search}`;
            respHeaders.set("Location", rewrittenLoc);
          }
        } catch (_) {
          // Keep original Location if unparseable
        }
      }

      // 8. Rewrite Set-Cookie headers: strip Domain and normalize Path=/
      if (typeof response.headers.getSetCookie === "function") {
        const rawCookies = response.headers.getSetCookie();
        if (rawCookies && rawCookies.length > 0) {
          respHeaders.delete("Set-Cookie");
          for (const c of rawCookies) {
            let stripped = c.replace(/;\s*Domain=[^;]+/gi, "");
            if (/;\s*Path=[^;]+/i.test(stripped)) {
              stripped = stripped.replace(/;\s*Path=[^;]+/gi, "; Path=/");
            } else {
              stripped += "; Path=/";
            }
            respHeaders.append("Set-Cookie", stripped);
          }
        }
      }

      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: respHeaders,
      });
    } catch (err) {
      return new Response(`Proxy Error: ${err.message}`, { status: 502 });
    }
  },
};

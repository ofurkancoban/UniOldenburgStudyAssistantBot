# Cloudflare Worker Reverse Proxy for Stud.IP & StuMS

This Cloudflare Worker acts as a distributed reverse proxy for Uni Oldenburg services (`studip.uni-oldenburg.de`, `elearning.uni-oldenburg.de`, `stums.uni-oldenburg.de`, `idp.uni-oldenburg.de`, `login.uni-oldenburg.de`).

It ensures that requests originating from the bot reach university servers from rotating Cloudflare edge IP addresses rather than directly from your server's single VPS IP.

## Features
- Does not follow redirects itself: the bot follows them hop by hop, so every hop is throttled and carries the right cookies.
- Moves every upstream `Set-Cookie` header, unmodified, to `X-Upstream-Set-Cookie`. The bot stores those cookies under the real university host (Domain and Path intact). The previous version stripped `Domain=` and forced `Path=/`, which put the `JSESSIONID` of StuMS and of the NetIQ login under one name on the worker's hostname, so they overwrote each other and StuMS never stayed signed in.
- Whitelists only official University of Oldenburg domains to prevent open-proxy abuse.
- Requires the shared `X-Proxy-Secret` token (requests are refused when `PROXY_SECRET` is not set), and drops Cloudflare/forwarding headers before contacting the university.

> After updating `worker.js`, redeploy the worker. The bot also still works with an old deployment, but only the current one keeps cookie domains exact.

---

## 1-Minute Deployment via Cloudflare Dashboard (Recommended)

1. Log in to [Cloudflare Dashboard](https://dash.cloudflare.com/).
2. Navigate to **Compute (Workers) -> Workers & Pages** in the left sidebar.
3. Click **Create Application** -> **Create Worker**.
4. Name the worker (e.g. `uni-oldenburg-proxy`) and click **Deploy**.
5. Click **Edit code** in the top right.
6. Delete the starter code, paste the contents of [`worker.js`](worker.js), and click **Deploy**.
7. Go to **Settings -> Variables and Secrets**:
   - Add an environment variable:
     - Variable name: `PROXY_SECRET`
     - Value: A strong random secret string (e.g. generated via `openssl rand -hex 24`)
     - Click **Deploy** / **Save**.
8. Copy your worker's public URL (e.g. `https://uni-oldenburg-proxy.<your-subdomain>.workers.dev`).

---

## Configure Bot `.env`

Add the following two lines to your `.env` file on your server:

```env
CLOUDFLARE_WORKER_URL=https://uni-oldenburg-proxy.<your-subdomain>.workers.dev
CLOUDFLARE_WORKER_SECRET=your_chosen_proxy_secret
```

Then restart the bot:
```bash
pm2 restart UniAssistant
```

If `CLOUDFLARE_WORKER_URL` is omitted or empty, the bot automatically makes direct connections without any proxy overhead.

---

## Alternative: Deployment via Wrangler CLI

With Node.js installed, use the project's pinned Wrangler version:

```bash
cd cloudflare_worker
npm ci
npx --no-install wrangler secret put PROXY_SECRET
npm run deploy
```

For a coordinated Worker and VPS release, run `scripts/deploy.sh --worker`
from the repository root. It installs the locked Worker dependencies and
restores the previous Worker deployment if the VPS release fails.

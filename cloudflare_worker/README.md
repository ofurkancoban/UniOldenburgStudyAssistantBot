# Cloudflare Worker Reverse Proxy for Stud.IP & StuMS

This Cloudflare Worker acts as a distributed reverse proxy for Uni Oldenburg services (`studip.uni-oldenburg.de`, `elearning.uni-oldenburg.de`, `stums.uni-oldenburg.de`, `idp.uni-oldenburg.de`, `login.uni-oldenburg.de`).

It ensures that requests originating from the bot reach university servers from rotating Cloudflare edge IP addresses rather than directly from your server's single VPS IP.

## Features
- Preserves NetIQ SSO redirects and full authentication flows.
- Rewrites `Set-Cookie` headers by stripping `Domain=` and setting `Path=/` so `aiohttp` maintains session cookies across routed endpoints.
- Whitelists only official University of Oldenburg domains to prevent open-proxy abuse.
- Protected by a shared `X-Proxy-Secret` token.

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

If you have Node.js and Wrangler installed locally:

```bash
cd cloudflare_worker
npm install -g wrangler
wrangler secret put PROXY_SECRET
wrangler deploy
```

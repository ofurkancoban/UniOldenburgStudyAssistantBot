# /login: Stud.IP login through the bot

`/login` opens the real Uni Oldenburg SSO pages (login.uni-oldenburg.de,
auth.uni-oldenburg.de, elearning.uni-oldenburg.de) proxied through the bot's
own session (`studip_login_server.py`). Whatever you sign in to there lands in
the bot's cookie jar, so the watchers keep working afterwards. The proxy gives
full access to the Stud.IP account, so it is locked down:

- Only Telegram users in `ALLOWED_USER_IDS` get in, checked through the signed
  Mini App `initData` or a single-use `/login/<token>` link (10 minutes).
  An empty `ALLOWED_USER_IDS` lets nobody in.
- The proxy cookie is HMAC-signed, expires after one hour regardless of use,
  and is invalidated by `/logout` and by every bot restart.
- The "Relink" action (logs the bot out and starts a fresh SSO login) needs a
  per-session CSRF token, so another website can't trigger it.

## Option 1: Cloudflare quick tunnel (no domain needed)

1. Install `cloudflared` on the server and make sure it is on `PATH`
   (or set `CLOUDFLARED_BIN=/path/to/cloudflared`).
2. In `.env`:

   ```ini
   CLOUDFLARE_TUNNEL=quick
   ```

3. Restart the bot. It starts `cloudflared tunnel --url http://127.0.0.1:8899`
   detached, reads the `https://<random>.trycloudflare.com` URL from its log
   and reuses the same tunnel across bot restarts. The PID and log are kept in
   `.cloudflared/` (mode 0700) next to the bot.

The hostname changes whenever cloudflared itself restarts; `/login` and the
menu button always use the current one.

## Option 2: your own hostname

Point an HTTPS hostname (e.g. via Cloudflare or nginx) at port `LOGIN_PORT`
(default 8899) on the server and set:

```ini
LOGIN_PUBLIC_URL=https://bot.example.com
```

## Option 3: LAN only (testing)

With neither of the above, `/login` sends a plain `http://<lan-ip>:8899/login/<token>`
link. The Telegram Mini App needs HTTPS, so this only works as a normal link
inside your own network.

## Bind address

The login server listens on `127.0.0.1` whenever a tunnel or `LOGIN_PUBLIC_URL`
is configured (the tunnel or reverse proxy is the only thing that needs to
reach it), and on `0.0.0.0` only in LAN mode. Override with `LOGIN_HOST`.

# Cookie extension listener

The `Cookie Sync` browser extension ([musicbot-cookie-sync](https://github.com/KeiraOMG0/musicbot-cookie-sync), MIT, by Keira) keeps the music cog's
`cookies.txt` fresh. The bot's side is `services/cookie_listener_service.py`, started and stopped
by the music cog (`cog_load` / `cog_unload`). It is **on by default** and speaks the **extension's
own API**, so **the extension needs no edits**.

## API (the extension's protocol)

- Default address `http://127.0.0.1:8999`.
- `GET /needs-refresh` -> `200 {"needsRefresh": true|false}` (`application/json`).
- `POST /cookies` with a Netscape `cookies.txt` body (`Content-Type: text/plain`) -> `200 ok`.
  Errors are plain text: `400` (`empty cookies payload` / `bad request`), `500`
  (`failed to write cookies file`), `404` for other paths.
- `OPTIONS` -> `204` with CORS headers.
- Additions on the bot's side: `X-Cookies-Accepted` / `X-Cookies-Rejected` response headers, and
  extra refusal codes (`401` auth, `403` bad origin, `413` too large, `422` nothing acceptable,
  `429` rate limit) that the stock extension simply treats as a failed push.

## How it works

1. The extension polls `GET /needs-refresh` every ~30 s.
2. When yt-dlp fails with a cookie/age error the music cog flags a refresh (waits up to 45 s, then
   retries the extraction once).
3. The extension reads the live `youtube.com` cookies and POSTs them.
4. The listener filters and validates them, then atomically replaces `MUSIC_COOKIES_FILE`
   (previous file kept as `cookies.txt.bak`). yt-dlp re-reads the file on every extraction.

## Auth and safety

- **`GET /needs-refresh` and `OPTIONS` are open** (no secret, no Origin needed). Chrome sends no
  `Origin` on the extension's poll, and the reply is only `{"needsRefresh": bool}`, so nothing leaks.
  A poll that does carry an `Origin` must still be an extension origin (a web page gets 403).
- **`POST /cookies` is strict.** Chrome always sends `Origin: chrome-extension://<id>` on a POST
  (Fetch attaches Origin to every non-GET cross-origin request; the stock extension POSTs
  `text/plain` via plain `fetch`).
  - *No secret set (default):* the POST must carry a `chrome-extension://` / `moz-extension://`
    `Origin` (optionally only ids in `MUSIC_COOKIE_LISTENER_EXTENSION_IDS`). A website cannot forge
    `Origin`, so web pages cannot POST to localhost. No `Origin` means 401.
  - *`MUSIC_COOKIE_LISTENER_SECRET` set (optional hardening):* `X-Cookie-Secret: <secret>` is required
    on POST (constant-time compare); the stock extension does not send it (see "Optional: secret").
- The first request from each extension id is logged once at INFO. To lock it down, copy that id
  into `MUSIC_COOKIE_LISTENER_EXTENSION_IDS`.
- Log lines: `Cookie extension connected (first poll received)` and `Cookies refreshed ...: N accepted`
  show it working. Rejections log a WARNING the first time per kind, then at most one per 10 minutes
  (with a count); the rest go to DEBUG. `!musicstatus` shows `Extension last seen: <age> ago`.
- Binds `127.0.0.1` by default (a non-loopback host logs a warning), body cap
  `MUSIC_COOKIE_LISTENER_MAX_BYTES` (512 KiB), `MUSIC_COOKIE_LISTENER_RATE_PER_MIN` (30).
- Only `youtube.com`, `google.com`, `googlevideo.com` cookies (and subdomains such as
  `accounts.google.com`) are kept; other lines are dropped and counted as rejected.
- Cookie values are never logged (counts and timestamps only). The log states once which auth mode
  is active.
- It never blocks or crashes the bot: if the port cannot be bound it logs one warning and retries in
  the background (backoff up to 60 s) until the port frees.

## Port conflicts

Only one program can listen on a port. If something else already holds 8999 (for example another
bot that uses the same extension), the listener logs a warning and keeps retrying in the background
until the port frees; nothing else is affected.

To run two such programs at once, give this bot a different port (`MUSIC_COOKIE_LISTENER_PORT=8977`)
and point a second copy of the extension at it (options page, "Server origin"). The extension talks
to one server at a time.

## Configuration

All optional; see `.env.example`: `MUSIC_COOKIE_LISTENER_ENABLED` (default `true`),
`_HOST`, `_PORT` (default 8999), `_SECRET`, `_EXTENSION_IDS`, `_MAX_BYTES`, `_RATE_PER_MIN`.
`!musicstatus` shows `Last cookie refresh: <age> ago` (staff also see counters and the last error).

## Optional: secret (hardening only)

Only if you set `MUSIC_COOKIE_LISTENER_SECRET`, edit your copy of the extension to send it. In `background.js` add `secret: ""` to `DEFAULTS`, read it in
`pollAndSync`, build `const auth = secret ? { "X-Cookie-Secret": secret } : {};` and pass
`{ headers: auth }` to the `/needs-refresh` fetch and `headers: { "Content-Type": "text/plain", ...auth }`
to the `/cookies` POST. Add a password input `id="secret"` to `options.html` and load/save it in
`options.js` alongside `serverOrigin` and `cookieDomain`. Leave the secret unset if you use the
extension unmodified.

## Troubleshooting

- `401` (POST only): secret mode and the header is missing/wrong, or no-secret mode and the POST had no
  extension `Origin`. `403`: the `Origin` is a web page or not in `_EXTENSION_IDS`.
- `422`: the pushed cookies contained nothing for the allowed domains. `429`: over the rate limit.
- "could not bind": another process holds the port; see "Port conflicts".
- `last_error` (staff `!musicstatus`) holds a short reason, never cookie contents.

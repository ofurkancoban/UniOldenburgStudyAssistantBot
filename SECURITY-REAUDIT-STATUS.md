# Security re-audit status

Status: **source fixes applied; runtime and deployment verification incomplete**.

This review covers the current dirty worktree. It does not establish the state of a deployed VPS. Target tests and builds were not executed because the required isolated execution controls are unavailable.

## Findings addressed in source

- **Admin callback revocation race:** `unibot/system.py` now rechecks current allowlist and admin role after Telegram awaits and before privileged output or process restart. Invite creation is still synchronous and follows the initial authorization check without an intervening await.
- **Broadcast sender revocation race:** `unibot/telegram_ui.py` accepts the initiating admin ID and rechecks it before each recipient/message send and after each awaited send. Both admin broadcast entrypoints pass the sender ID. Existing recipient allowlist, active-state, private-context, and course-membership checks remain enforced.
- **Proxy HTML protections:** upstream file responses identified by `Content-Disposition` or a non-redirect `/sendfile.php` response now stream as `application/octet-stream`, `attachment`, and `nosniff`. Ordinary proxy HTML retains upstream CSP script/resource rules while replacing only frame policy for Telegram. This reduces risk but does not establish that upstream sanitizes user content; normal HTML pages remain a validation item.
- **Shared authorization invariant:** `auth.private_request_is_current()` centralizes allowlist and private-request-generation checks; broadcast recipient delivery uses it at the send boundary.
- **Security event trail:** admin callback denials, account/role/session changes, and personal-data deletion outcomes emit bounded event names. IDs are omitted unless `SECURITY_AUDIT_KEY` is configured with at least 32 bytes, in which case only keyed HMAC tags are logged. The event logger accepts no message bodies, course names, URLs or credentials.
- **CI permissions and time bounds:** GitHub Actions now has repository read-only permissions and explicit job timeouts. CI already runs the full Python test suite, lint/type checks, Node syntax checks, and dependency audit.

Regression coverage was added in `tests/test_reaudit_regressions.py` for sender demotion during fan-out, callback demotion during an awaited answer, file-download header enforcement, current private-request context, and pseudonymous audit records. Existing suites also cover cross-user private storage and course-content membership isolation. These tests are **not run**. Source review is not a substitute for the required isolated execution.

## Static checks completed

- `git diff --check` completed with no whitespace errors.
- Broadcast call sites were inspected. User-triggered admin sends pass sender identity; watcher-generated automated notices do not have an initiating admin.
- Source confirms configurable admin IDs, proxy cookie `HttpOnly` and HTTPS-dependent `Secure`, and named Cloudflare tunnel configuration checks. These do not verify deployed values or infrastructure behavior.

## Still needs owner or sandbox validation

1. Run the added regression tests and the project suite in an OS-enforced sandbox with network denial, an empty allowlisted environment, read-only source/toolchain, scratch-only writes, and explicit resource limits.
2. Establish whether lower-trust Stud.IP course content can be published and served as `text/html` in normal application pages, and inspect those pages' effective CSP in an owner-controlled browser. Upstream CSP is preserved except for frame policy, but missing/weak script policy or unsafe content rendering remains unresolved.
3. On the VPS, verify the deployed commit, effective `ADMIN_USER_IDS` and `ALLOWED_USER_IDS`, `LOGIN_HOST`/public URL, HTTPS termination, Cloudflare tunnel mode and secret binding, service ownership and file permissions, and backup copies. Do not share secret values; report only whether each expected binding/control is present.
4. Confirm provider retention/processing settings and the upstream Stud.IP response headers using an owner-controlled benign test resource. No live endpoint was contacted in this review.
5. Configure a unique random `SECURITY_AUDIT_KEY` of at least 32 bytes per deployment if operators need to correlate security events over time; do not reuse Telegram or provider tokens.

No absolute security guarantee is claimed. The earlier audit artifacts under the external security-audit-skill directory describe the pre-fix source and should not be treated as current; this workspace status file records the source changes and remaining gaps.

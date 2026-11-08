"""The NetIQ / Uni Oldenburg SSO state machine, shared by the Stud.IP login
and the hand-off to other relying parties such as StuMS.

`SsoFlowMixin` is mixed into StudIPSession and only relies on its HTTP layer
(`_s_get`, `_s_post`), credentials and logger; studip_session keeps the
transport, cookie storage and session lifecycle.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from studip_utils import redact_url, tag_attr, unroute_proxy_url


def _describe_page(soup, text: str) -> str:
    """Structure of an unrecognised SSO page for the log: title, forms with
    their action path and input names/types, and script hints. Never values."""
    try:
        title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:80]
        forms = []
        for form in soup.find_all("form")[:4]:
            fields = ",".join(
                f"{inp.get('name') or inp.get('id') or '?'}:{inp.get('type') or 'text'}"
                for inp in form.find_all(["input", "button", "select"])[:12]
            )
            forms.append(f"{(form.get('method') or 'get').upper()} {redact_url(form.get('action') or '')} [{fields}]")
        hints = [h for h in ("submit()", "location", "onload", "setTimeout", "fetch(", "XMLHttpRequest")
                 if h in text]
        snippet = ""
        if not forms and len(text) <= 300:
            # a tiny formless reply is an IdP error message, safe to show
            snippet = " body=" + repr(re.sub(r"\s+", " ", text).strip()[:200])
        return f"title={title!r} forms={forms} scripts={hints} len={len(text)}{snippet}"
    except Exception as exc:
        return f"<page not described: {exc}>"


class SsoFlowMixin:
    """SSO login steps for StudIPSession (see the class docstring there)."""

    if TYPE_CHECKING:
        # Provided by StudIPSession; declared here for the type checker only.
        username: str
        password: str
        logger: logging.Logger
        explicitly_logged_out: bool
        last_interactive_activity: float

        def _s_get(self, url: str, **kwargs: Any) -> Any: ...
        def _s_post(self, url: str, **kwargs: Any) -> Any: ...
        def _get_totp_token(self) -> Optional[str]: ...
        async def ensure_session(self) -> Any: ...
        async def save_cookies(self) -> None: ...
        async def is_html_logged_in(self, use_cache: bool = True) -> bool: ...

    async def sso_handoff(self, entry_url: str, success_host: str, referer: Optional[str] = None) -> str:
        """Run the same NetIQ SSO state machine as `login()`, but to reach a different
        relying party (e.g. stums.uni-oldenburg.de) instead of elearning.uni-oldenburg.de.

        A valid elearning session does not guarantee this succeeds in one plain
        HTTP redirect: the underlying NetIQ SSO trust can have its own, independent
        timeout, in which case this transparently falls through to the very same
        credential-entry steps `login()` uses (Ecom fields, TOTP/contract page).

        Returns the final URL once `success_host` is reached, or raises RuntimeError.
        """
        if self.explicitly_logged_out or (time.time() - self.last_interactive_activity < 180):
            raise RuntimeError("Interactive login or logout active; skipping background SSO handoff.")
        await self.ensure_session()

        async def is_done(url, _text):
            u = urlparse(url)
            if u.netloc.lower() == success_host.lower():
                if "/qisserver/" in u.path:
                    return "/callback/" not in u.path and "error" not in u.path.lower()
                return True
            return False

        final_url, _ = await self._run_sso_flow(entry_url, referer=referer, is_done=is_done)
        await self.save_cookies()
        return final_url

    async def _run_sso_flow(self, entry_url: str, referer: Optional[str] = None, is_done=None):
        """Shared NetIQ SSO redirect + credential-entry state machine.

        `is_done(url, text)` decides when the flow has reached its destination;
        defaults to `self.is_logged_in()` (elearning-specific) when omitted, which
        is what `login()` uses. Returns (final_url, final_text) on success, raises
        RuntimeError if the flow gets stuck or exhausts its step budget.
        """
        async def follow_redirects(url, text, tag, depth=0):
                if depth > 20: 
                    self.logger.warning("[%s] Max redirect depth reached at %s", tag, redact_url(url))
                    return url, text
                
                soup = BeautifulSoup(text, "html.parser")

                # 1. JS Redirect
                js_redirect = re.search(r"window\.location\.(?:href|assign|replace)\s*=\s*['\"]([^'\"]+)['\"]", text)
                if js_redirect:
                    nxt = urljoin(url, js_redirect.group(1))
                    self.logger.info("[%s] Following JS redirect: %s", tag, redact_url(nxt))
                    async with self._s_get(nxt, headers={"Referer": url}) as r:
                         return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)

                # 2. Meta Refresh
                meta = soup.find("meta", attrs={"http-equiv": lambda x: (x or "").lower() == "refresh"})
                if meta:
                    content = tag_attr(meta, "content")
                    if "url=" in content.lower():
                        nxt = urljoin(url, content.lower().split("url=")[1].strip().strip("'\""))
                        self.logger.info("[%s] Following Meta Refresh: %s", tag, redact_url(nxt))
                        async with self._s_get(nxt, headers={"Referer": url}) as r:
                            return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)
                
                # 3. AJAX Content Load (NetIQ specific)
                ajax_load = re.search(r"getToContent\(['\"]([^'\"]+)['\"]", text)
                if ajax_load:
                    ajax_url = urljoin(url, ajax_load.group(1))
                    self.logger.info("[%s] Following NetIQ AJAX content load: %s", tag, redact_url(ajax_url))
                    async with self._s_get(ajax_url, headers={"Referer": url}) as r:
                         return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)
                
                # 4. Script-based form auto-submit (e.g., NetIQ authCodeForm):
                #    document.forms['x'].submit(), document.x.submit(),
                #    document.getElementById('x').submit(), forms[0].submit()
                form_submit = re.search(
                    r"document\.(?:forms\[['\"]?([^'\"\]]+)['\"]?\]|getElementById\(['\"]([^'\"]+)['\"]\)|([a-zA-Z0-9_]+))\s*\.\s*submit\(\)",
                    text,
                )
                if form_submit:
                    form_name = form_submit.group(1) or form_submit.group(2) or form_submit.group(3)
                    form = soup.find("form", attrs={"name": form_name}) or soup.find("form", id=form_name)
                    if form is None and form_name.isdigit():
                        forms = soup.find_all("form")
                        form = forms[int(form_name)] if int(form_name) < len(forms) else None
                    if form is not None and form.find("input", attrs={"type": "password"}):
                        form = None  # never auto-submit a credential form
                    if form:
                        inputs = {i.get("name"): (i.get("value") or "") for i in form.find_all("input") if i.get("name")}
                        action = urljoin(url, tag_attr(form, "action"))
                        method = (tag_attr(form, "method") or "POST").upper()
                        self.logger.info("[%s] Script-submitting form '%s' to %s", tag, form_name, redact_url(action))
                        if method == "POST":
                            async with self._s_post(action, data=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)
                        else:
                            async with self._s_get(action, params=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)

                # 5. OAuth/SAML Auth Handoff Forms (Auto-submit via presence of sensitive keys)
                for form in soup.find_all("form"):
                    inputs = {i.get("name"): (i.get("value") or "") for i in form.find_all("input") if i.get("name")}
                    keys = {tag_attr(i, "name").lower() for i in form.find_all("input") if i.get("name")}
                    if keys & {"code", "state", "id_token", "samlresponse", "relaystate", "wa", "wresult"}:
                        action = urljoin(url, tag_attr(form, "action"))
                        method = (tag_attr(form, "method") or "POST").upper()
                        self.logger.info("[%s] Auto-submitting auth handoff form to %s", tag, redact_url(action))
                        if method == "POST":
                            async with self._s_post(action, data=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)
                        else:
                            async with self._s_get(action, params=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)

                self.logger.debug("[%s] No more redirects detected at %s", tag, redact_url(url))
                return url, text

        # Helper for form submission
        def get_payload(soup, overrides=None):
            form = soup.find("form")
            if not form: return None, None, None
            action = form.get("action") or ""
            method = (form.get("method") or "POST").upper()
            inputs = {}
            for inp in form.find_all("input"):
                name = inp.get("name")
                if name:
                    inputs[name] = inp.get("value") or ""
            if overrides:
                inputs.update(overrides)
            return action, method, inputs

        async def check_done(url, text):
            if is_done:
                return await is_done(url, text)
            return await self.is_html_logged_in(use_cache=False)

        # 1. Entry
        async with self._s_get(entry_url, headers={"Referer": referer} if referer else None) as r:
            curr_url, curr_text = await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), "entry")

        # 2. Main state-machine loop
        prev_text = ""
        for i in range(15):
            if curr_text == prev_text:
                self.logger.warning("Page content stayed identical. Breaking loop.")
                break
            prev_text = curr_text

            soup = BeautifulSoup(curr_text, "html.parser")
            page_text = curr_text.lower()

            self.logger.info("Step %s: URL %s", i, redact_url(curr_url))

            if await check_done(curr_url, curr_text):
                return curr_url, curr_text

            # Identification Logic

            # A. Legacy Login Page (Ecom fields)
            if "ecom_user_id" in page_text and "ecom_password" not in page_text:
                if not self.username:
                    raise RuntimeError("Interactive login required (no stored username)")
                self.logger.info("Step %s: Username Submission", i+1)
                action, method, payload = get_payload(soup, {"Ecom_User_ID": self.username, "loginButton2": "true"})
                action = urljoin(curr_url, action)
                async with self._s_post(action, data=payload, headers={"Referer": curr_url}) as r:
                    curr_url, curr_text = await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), "user")
                continue

            if "ecom_password" in page_text:
                if not self.password:
                    raise RuntimeError("Interactive login required (no stored password)")
                self.logger.info("Step %s: Password Submission (Legacy)", i+1)
                action, method, payload = get_payload(soup, {"Ecom_Password": self.password, "loginButton2": "true"})
                action = urljoin(curr_url, action)
                async with self._s_post(action, data=payload, headers={"Referer": curr_url}) as r:
                    curr_url, curr_text = await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), "pass")
                continue

            # B. contract Page (Unified)
            if "/osp/a/TOP/auth/app/contract" in curr_url or "/osp/a/TOP/auth/app/contract" in curr_text:
                title_area = soup.find(id="authenticationAreaTitle")
                title_text = title_area.get_text().lower() if title_area else ""
                # Check if it's OTP based on title OR specific text
                is_otp = any(x in title_text for x in ["one time", "otp", "code", "authenticator"])
                # If title doesn't help, check whole page but more conservatively
                if not is_otp and not ("password" in title_text):
                    is_otp = any(x in page_text for x in ["one time password", "totp code", "google authenticator"])

                if is_otp:
                    token = self._get_totp_token()
                    if not token:
                        raise RuntimeError("Interactive login required (no 2FA token available)")
                    self.logger.info("Step %s: TOTP Submission (Contract)", i+1)
                    action, method, payload = get_payload(soup, {"nffc": token, "loginButton2": "Next"})
                else:
                    if not self.password:
                        raise RuntimeError("Interactive login required (no stored password)")
                    self.logger.info("Step %s: Password Submission (Contract)", i+1)
                    action, method, payload = get_payload(soup, {"nffc": self.password, "loginButton2": "Next"})

                action = urljoin(curr_url, action)
                async with self._s_post(action, data=payload, headers={"Referer": curr_url}) as r:
                    curr_url, curr_text = await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), "contract")
                continue

            # C. Unhandled
            self.logger.warning("Step %s: Unhandled state. URL: %s | %s", i, redact_url(curr_url),
                                _describe_page(soup, curr_text))
            if i > 10: break

        if await check_done(curr_url, curr_text):
            return curr_url, curr_text
        raise RuntimeError(f"SSO flow did not complete; stuck at {curr_url}")

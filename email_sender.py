"""
email_sender.py
===============

All outbound email logic lives here. Two transports are supported:

* SMTP  - via the standard library `smtplib` (run in a worker thread so it
          does not block the asyncio event loop).
* API   - Brevo, Resend and SendGrid over HTTPS using `httpx`.

Every public function returns *normalized* result objects so bot.py never
has to know which provider was used:

    TestResult   - outcome of a connection / credential check
    SendResult   - outcome of sending one email
    SendSummary  - aggregate of a whole campaign

Result categories (as required by the spec):
    SENT      - server accepted the message (2xx)
    REJECTED  - permanent failure for this recipient (5xx)
    FAILED    - auth errors, timeouts, rate limits, temporary 4xx
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
import certifi
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Iterable, Literal, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

Category = Literal["sent", "rejected", "failed"]

# How long (seconds) to wait on network operations before giving up.
SMTP_TIMEOUT = 20
HTTP_TIMEOUT = 20

# Provider metadata: endpoint + how to authenticate.
API_PROVIDERS = {
    "brevo": {
        "label": "Brevo",
        "url": "https://api.brevo.com/v3/smtp/email",
        "auth_header": "api-key",
    },
    "resend": {
        "label": "Resend",
        "url": "https://api.resend.com/emails",
        "auth_header": "Authorization",  # "Bearer <key>"
    },
    "sendgrid": {
        "label": "SendGrid",
        "url": "https://api.sendgrid.com/v3/mail/send",
        "auth_header": "Authorization",  # "Bearer <key>"
    },
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    """Outcome of a connection / credential test."""

    ok: bool
    message: str  # human readable, shown to the user


@dataclass
class SendResult:
    """Outcome of sending to a single recipient."""

    recipient: str
    category: Category
    reason: str = ""  # error detail for rejected/failed


@dataclass
class SendSummary:
    """Aggregate result of a campaign."""

    sent: list[SendResult] = field(default_factory=list)
    rejected: list[SendResult] = field(default_factory=list)
    failed: list[SendResult] = field(default_factory=list)
    # True when we could not even connect/authenticate -> nothing was sent.
    sender_issue: bool = False
    sender_issue_reason: str = ""

    @property
    def total(self) -> int:
        return len(self.sent) + len(self.rejected) + len(self.failed)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _bucket(summary: SendSummary, category: Category) -> list[SendResult]:
    """Return the list inside `summary` that matches a category."""
    if category == "sent":
        return summary.sent
    if category == "rejected":
        return summary.rejected
    return summary.failed


def _classify_smtp_error(exc: BaseException) -> Category:
    """
    Map an SMTP exception to our three categories.

    smtplib raises SMTPRecipientsRefused with a dict of
    {recipient: (code, message)}. Codes 5xx are permanent (rejected),
    4xx are temporary (failed).
    """
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in exc.recipients.values()]
        if codes and all(500 <= code < 600 for code in codes):
            return "rejected"
        return "failed"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        # Our own sender address was refused - that is a sender problem.
        return "failed"
    if isinstance(exc, smtplib.SMTPDataError):
        code = exc.smtp_code
        return "rejected" if 500 <= code < 600 else "failed"
    return "failed"


def _smtp_error_text(exc: BaseException) -> str:
    """Produce a short, readable error string for the UI."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        parts = []
        for rcpt, (code, msg) in exc.recipients.items():
            if isinstance(msg, bytes):
                msg = msg.decode(errors="replace")
            parts.append(f"{code} {msg}")
        return "; ".join(parts) or "Recipient refused"
    if isinstance(exc, smtplib.SMTPResponseException):
        msg = exc.smtp_error
        if isinstance(msg, bytes):
            msg = msg.decode(errors="replace")
        return f"{exc.smtp_code} {msg}"
    return f"{type(exc).__name__}: {exc}"


def _build_message(
    *,
    sender: str,
    sender_name: str,
    recipient: str,
    subject: str,
    body: str,
    is_html: bool,
) -> EmailMessage:
    """Build a MIME message. HTML bodies get a plain-text fallback."""
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    if is_html:
        msg.set_content("This message contains HTML. Please use an HTML-capable client.")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    return msg


# ---------------------------------------------------------------------------
# SMTP - blocking implementation (runs in a thread)
# ---------------------------------------------------------------------------

def _smtp_test_blocking(host: str, port: int, username: str, password: str) -> TestResult:
    """
    Connect, STARTTLS, login, quit. No email is sent.

    Port 465 is treated as implicit TLS (SMTPS); everything else uses
    plain connection + STARTTLS, which is what Gmail/Outlook/most hosts want.
    """
    try:
        if port == 465:
         server = smtplib.SMTP_SSL(
    host, port, timeout=SMTP_TIMEOUT, context=ssl.create_default_context(cafile=certifi.where())
)
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        try:
            server.login(username, password)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - quit failures are irrelevant
                pass
        return TestResult(True, "Connection and login succeeded.")
    except smtplib.SMTPAuthenticationError as exc:
        return TestResult(False, f"Login rejected: {_smtp_error_text(exc)}")
    except smtplib.SMTPException as exc:
        return TestResult(False, f"SMTP error: {_smtp_error_text(exc)}")
    except (OSError, ssl.SSLError) as exc:
        return TestResult(False, f"Connection failed: {exc}")
    except Exception as exc:  # noqa: BLE001 - never crash the bot
        return TestResult(False, f"Unexpected error: {exc}")


def _smtp_send_blocking(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    sender: str,
    sender_name: str,
    recipients: list[str],
    subject: str,
    body: str,
    is_html: bool,
) -> SendSummary:
    """
    Connect once, authenticate once, then send one message per recipient.

    Sending individually (instead of one message with many To: headers)
    lets us attribute success/failure to each address, which the UI needs.
    """
    summary = SendSummary()

    # --- Step 1: connect + authenticate. If this fails, nothing is sent. ---
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(
                host, port, timeout=SMTP_TIMEOUT, context=ssl.create_default_context()
            )
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        server.login(username, password)
    except Exception as exc:  # noqa: BLE001
        summary.sender_issue = True
        summary.sender_issue_reason = _smtp_error_text(exc)
        return summary

    # --- Step 2: send to each recipient. ---
    try:
        for index, rcpt in enumerate(recipients):
            msg = _build_message(
                sender=sender,
                sender_name=sender_name,
                recipient=rcpt,
                subject=subject,
                body=body,
                is_html=is_html,
            )
            try:
                server.send_message(msg, from_addr=sender, to_addrs=[rcpt])
                summary.sent.append(SendResult(rcpt, "sent"))
            except smtplib.SMTPRecipientsRefused as exc:
                category = _classify_smtp_error(exc)
                _bucket(summary, category).append(
                    SendResult(rcpt, category, _smtp_error_text(exc))
                )
            except smtplib.SMTPServerDisconnected:
                # Connection dropped mid-campaign: everything left is failed.
                summary.failed.append(
                    SendResult(rcpt, "failed", "Server disconnected")
                )
                for rest in recipients[index + 1:]:
                    summary.failed.append(
                        SendResult(rest, "failed", "Not attempted - connection lost")
                    )
                break
            except smtplib.SMTPException as exc:
                category = _classify_smtp_error(exc)
                _bucket(summary, category).append(
                    SendResult(rcpt, category, _smtp_error_text(exc))
                )
            except Exception as exc:  # noqa: BLE001
                summary.failed.append(
                    SendResult(rcpt, "failed", f"{type(exc).__name__}: {exc}")
                )
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass

    return summary


# ---------------------------------------------------------------------------
# API providers - async implementation
# ---------------------------------------------------------------------------

def _api_headers(provider: str, api_key: str) -> dict[str, str]:
    """Build auth headers for the given provider."""
    meta = API_PROVIDERS[provider]
    if meta["auth_header"] == "api-key":
        return {"api-key": api_key, "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _api_payload(
    provider: str,
    *,
    sender: str,
    sender_name: str,
    recipient: str,
    subject: str,
    body: str,
    is_html: bool,
) -> dict:
    """Build the provider-specific JSON body for one email."""
    if provider == "brevo":
        return {
            "sender": {"email": sender, "name": sender_name},
            "to": [{"email": recipient}],
            "subject": subject,
            "htmlContent" if is_html else "textContent": body,
        }
    if provider == "resend":
        return {
            "from": f"{sender_name} <{sender}>",
            "to": [recipient],
            "subject": subject,
            "html" if is_html else "text": body,
        }
    if provider == "sendgrid":
        content_type = "text/html" if is_html else "text/plain"
        return {
            "personalizations": [{"to": [{"email": recipient}]}],
            "from": {"email": sender, "name": sender_name},
            "subject": subject,
            "content": [{"type": content_type, "value": body}],
        }
    raise ValueError(f"Unknown API provider: {provider}")


def _classify_http_status(status: int) -> Category:
    """2xx -> sent, 5xx -> rejected, everything else -> failed."""
    if 200 <= status < 300:
        return "sent"
    if 500 <= status < 600:
        return "rejected"
    return "failed"


def _http_error_text(response: httpx.Response) -> str:
    """Extract a short error message from a provider response."""
    try:
        data = response.json()
        # Providers use different keys; try the common ones.
        for key in ("message", "error", "errors", "detail"):
            if key in data:
                value = data[key]
                if isinstance(value, list):
                    value = "; ".join(str(v) for v in value)
                return f"{response.status_code} {value}"
    except Exception:  # noqa: BLE001 - body may not be JSON
        pass
    text = response.text.strip().replace("\n", " ")
    return f"{response.status_code} {text[:200]}"


async def _api_test(provider: str, api_key: str) -> TestResult:
    """
    Lightweight credential check.

    We deliberately avoid sending an email. Instead we call a read-only
    endpoint that requires the key:

      * Brevo    -> GET /v3/account
      * Resend   -> GET /domains
      * SendGrid -> GET /v3/scopes

    401/403 means the key is bad; anything else that is not a network error
    means the key was accepted.
    """
    endpoints = {
        "brevo": "https://api.brevo.com/v3/account",
        "resend": "https://api.resend.com/domains",
        "sendgrid": "https://api.sendgrid.com/v3/scopes",
    }
    url = endpoints[provider]
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=_api_headers(provider, api_key))
    except httpx.HTTPError as exc:
        return TestResult(False, f"Network error: {exc}")

    if resp.status_code in (401, 403):
        return TestResult(False, f"API key rejected: {_http_error_text(resp)}")
    if 200 <= resp.status_code < 300:
        return TestResult(True, "API key accepted.")
    # Some providers return 400/404 for endpoints not enabled on the plan,
    # but a bad key would have produced 401/403 above.
    return TestResult(
        True,
        f"API key accepted (endpoint returned {resp.status_code}).",
    )


async def _api_send(
    provider: str,
    api_key: str,
    *,
    sender: str,
    sender_name: str,
    recipients: list[str],
    subject: str,
    body: str,
    is_html: bool,
) -> SendSummary:
    """
    Send one HTTP request per recipient.

    We do not have a separate "authenticate" step with HTTP APIs, so the
    first request doubles as the auth check: if it returns 401/403 we mark
    the whole campaign as a sender issue and stop.
    """
    summary = SendSummary()
    url = API_PROVIDERS[provider]["url"]
    headers = _api_headers(provider, api_key)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for index, rcpt in enumerate(recipients):
            payload = _api_payload(
                provider,
                sender=sender,
                sender_name=sender_name,
                recipient=rcpt,
                subject=subject,
                body=body,
                is_html=is_html,
            )
            try:
                resp = await client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                summary.failed.append(
                    SendResult(rcpt, "failed", f"Network error: {exc}")
                )
                continue

            if resp.status_code in (401, 403):
                # Credentials are wrong -> nothing else will work either.
                summary.sender_issue = True
                summary.sender_issue_reason = _http_error_text(resp)
                # Everything not yet attempted counts as failed.
                for rest in recipients[index:]:
                    summary.failed.append(
                        SendResult(rest, "failed", "Not attempted - authentication failed")
                    )
                return summary

            category = _classify_http_status(resp.status_code)
            if category == "sent":
                summary.sent.append(SendResult(rcpt, "sent"))
            else:
                _bucket(summary, category).append(
                    SendResult(rcpt, category, _http_error_text(resp))
                )

    return summary


# ---------------------------------------------------------------------------
# Public async API used by bot.py
# ---------------------------------------------------------------------------

async def test_smtp(host: str, port: int, username: str, password: str) -> TestResult:
    """Async wrapper around the blocking SMTP test."""
    return await asyncio.to_thread(
        _smtp_test_blocking, host, port, username, password
    )


async def test_api(provider: str, api_key: str) -> TestResult:
    """Validate an API key without sending mail."""
    if provider not in API_PROVIDERS:
        return TestResult(False, f"Unknown provider: {provider}")
    return await _api_test(provider, api_key)


async def send_emails(
    *,
    config_type: str,
    sender: str,
    sender_name: str,
    recipients: Iterable[str],
    subject: str,
    body: str,
    is_html: bool,
    secret: str,
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
    smtp_username: Optional[str] = None,
    api_provider: Optional[str] = None,
) -> SendSummary:
    """
    Send to every recipient using the given (already decrypted) config.

    `secret` is the plaintext SMTP password or API key.
    """
    rcpt_list = list(recipients)
    if not rcpt_list:
        return SendSummary()

    if config_type == "smtp":
        if not (smtp_host and smtp_port and smtp_username):
            return SendSummary(
                sender_issue=True,
                sender_issue_reason="Incomplete SMTP configuration",
            )
        return await asyncio.to_thread(
            _smtp_send_blocking,
            host=smtp_host,
            port=smtp_port,
            username=smtp_username,
            password=secret,
            sender=sender,
            sender_name=sender_name,
            recipients=rcpt_list,
            subject=subject,
            body=body,
            is_html=is_html,
        )

    if config_type == "api":
        if not api_provider:
            return SendSummary(
                sender_issue=True,
                sender_issue_reason="Incomplete API configuration",
            )
        return await _api_send(
            api_provider,
            secret,
            sender=sender,
            sender_name=sender_name,
            recipients=rcpt_list,
            subject=subject,
            body=body,
            is_html=is_html,
        )

    return SendSummary(
        sender_issue=True, sender_issue_reason=f"Unknown config type: {config_type}"
    )

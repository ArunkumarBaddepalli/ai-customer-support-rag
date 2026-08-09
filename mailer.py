"""Transactional email — password reset and email-verification links.
Flask cannot deliver mail on its own; this is the one external service that
actually puts a message in an inbox.

Two providers are supported, chosen by whichever key is set:

  BREVO_API_KEY   Brevo (brevo.com). 300 emails/day free, and it delivers to
                  *any* recipient once you verify a single sender address by
                  clicking a link in that inbox. No domain, no DNS, no card.
  RESEND_API_KEY  Resend. Kept because the app shipped on it, but its shared
                  sandbox sender only reaches the address the Resend account
                  was created with, so signups from other addresses silently
                  get nothing. A verified domain lifts that — a domain is the
                  blocker, not the code.

Brevo wins when it is set, because "reaches everyone" beats "reaches me".

With neither key set, sends are logged to stdout instead of failing, so local
development and the test suite never need a real account.
"""

import os
import re

import httpx

BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()

# The From address. With Brevo this must be a sender you have verified in the
# dashboard, otherwise every send is rejected with "sender not valid".
MAIL_FROM = (
    os.environ.get("MAIL_FROM", "").strip()
    or os.environ.get("RESEND_FROM", "").strip()
    or "SupportBot <onboarding@resend.dev>"
)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
RESEND_API_URL = "https://api.resend.com/emails"

TIMEOUT = 10


def _parse_from(value):
    """'SupportBot <hi@x.com>' -> ('SupportBot', 'hi@x.com').

    Resend takes the combined string; Brevo wants the two parts separately.
    """
    match = re.match(r"^\s*(.*?)\s*<\s*([^>]+?)\s*>\s*$", value)
    if match:
        return (match.group(1).strip('" ') or "SupportBot"), match.group(2)
    return "SupportBot", value.strip()


def _send_brevo(to, subject, html, text):
    name, address = _parse_from(MAIL_FROM)
    return httpx.post(
        BREVO_API_URL,
        headers={"api-key": BREVO_API_KEY, "accept": "application/json"},
        json={
            "sender": {"name": name, "email": address},
            "to": [{"email": to}],
            "subject": subject,
            "htmlContent": html,
            "textContent": text,
        },
        timeout=TIMEOUT,
    )


def _send_resend(to, subject, html, text):
    return httpx.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={"from": MAIL_FROM, "to": [to], "subject": subject,
              "html": html, "text": text},
        timeout=TIMEOUT,
    )


def active_provider():
    """Which provider will actually be used, for /healthz and startup logs."""
    if BREVO_API_KEY:
        return "brevo"
    if RESEND_API_KEY:
        return "resend"
    return "console"


def send_email(to, subject, html, text=""):
    """Best-effort send. Returns True on success, False otherwise.

    Callers must never let a failed send break the request — signup still
    succeeds even when the verification email didn't go out, because an
    account the user cannot reach is better than no account at all.
    """
    provider = active_provider()
    if provider == "console":
        print(f"[mailer] no provider configured — would send to {to!r}: {subject!r}")
        return False

    send = _send_brevo if provider == "brevo" else _send_resend
    try:
        response = send(to, subject, html, text or _strip_html(html))
        response.raise_for_status()
        print(f"[mailer] sent {subject!r} to {to!r} via {provider}")
        return True
    except httpx.HTTPStatusError as exc:
        # The response body is the only place that says *why* — "sender not
        # valid", "unauthorised", "recipient blocked". Without it every
        # failure reads as an opaque status code and costs an hour to debug.
        body = exc.response.text[:300]
        print(f"[mailer] {provider} rejected send to {to!r}: "
              f"HTTP {exc.response.status_code} {body}")
        return False
    except Exception as exc:
        print(f"[mailer] send to {to!r} via {provider} failed: "
              f"{type(exc).__name__}: {exc}")
        return False


def _strip_html(html):
    """Crude HTML -> text fallback for the plain-text alternative.

    A multipart message with a text part is scored noticeably better by spam
    filters than HTML alone, which matters more here than fidelity — the
    templates are a heading, a sentence and a link.
    """
    text = re.sub(r"<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2: \1", html,
                  flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]*\n\s*", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def _button(url, label):
    return (
        f'<a href="{url}" style="display:inline-block;background:#4f46e5;color:#fff;'
        f'text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:600;'
        f'font-family:sans-serif;">{label}</a>'
    )


def send_password_reset(to, reset_url):
    html = f"""
    <div style="font-family:sans-serif;color:#111;max-width:480px;margin:0 auto">
      <h2>Reset your password</h2>
      <p>Someone asked to reset the password on this account. If that was you:</p>
      <p>{_button(reset_url, "Reset password")}</p>
      <p style="color:#666;font-size:13px">This link expires in 30 minutes.
      If you didn't request this, you can ignore this email.</p>
    </div>
    """
    text = (
        "Reset your password\n\n"
        "Someone asked to reset the password on this account. If that was you, "
        f"open this link:\n{reset_url}\n\n"
        "The link expires in 30 minutes. If you didn't request this, ignore this email."
    )
    return send_email(to, "Reset your password", html, text)


def send_verification(to, verify_url):
    html = f"""
    <div style="font-family:sans-serif;color:#111;max-width:480px;margin:0 auto">
      <h2>Confirm your email</h2>
      <p>Click below to confirm this address for your SupportBot account.</p>
      <p>{_button(verify_url, "Confirm email")}</p>
      <p style="color:#666;font-size:13px">This link expires in 24 hours.</p>
    </div>
    """
    text = (
        "Confirm your email\n\n"
        "Open this link to confirm this address for your SupportBot account:\n"
        f"{verify_url}\n\nThe link expires in 24 hours."
    )
    return send_email(to, "Confirm your email", html, text)


if __name__ == "__main__":
    # Send a real test message:  python mailer.py you@example.com
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    # Re-read after load_dotenv — the module-level reads happened at import,
    # before .env was on the environment.
    BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
    MAIL_FROM = (os.environ.get("MAIL_FROM", "").strip()
                 or os.environ.get("RESEND_FROM", "").strip() or MAIL_FROM)

    if len(sys.argv) < 2:
        raise SystemExit("Usage: python mailer.py <recipient-email>")

    print(f"provider: {active_provider()}   from: {MAIL_FROM}")
    ok = send_verification(sys.argv[1], "http://localhost:5001/verify-email/test-token")
    print("sent" if ok else "FAILED — see the error above")
    raise SystemExit(0 if ok else 1)

"""Transactional email via Resend (resend.com) — password reset and
email-verification links. Flask/Python cannot deliver email on their own;
this is the one external service that actually puts a message in an inbox.

Without RESEND_API_KEY set, sends are logged to stdout instead of failing,
so local development and tests never need a real account.
"""

import os

import httpx

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
# Resend's shared sandbox sender. It works with zero setup, but can only
# deliver to the email address the Resend account itself was created with —
# verifying a custom domain (free, just a DNS record) lifts that limit.
RESEND_FROM = os.environ.get("RESEND_FROM", "SupportBot <onboarding@resend.dev>")

RESEND_API_URL = "https://api.resend.com/emails"


def send_email(to, subject, html):
    """Best-effort send. Returns True on success, False otherwise — callers
    should never let a failed send break the request (e.g. signup should
    still succeed even if the verification email didn't go out)."""
    if not RESEND_API_KEY:
        print(f"[mailer] RESEND_API_KEY not set — would send to {to!r}: {subject!r}")
        return False
    try:
        r = httpx.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": RESEND_FROM, "to": [to], "subject": subject, "html": html},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        print(f"[mailer] send to {to!r} failed: {type(exc).__name__}: {exc}")
        return False


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
    return send_email(to, "Reset your password", html)


def send_verification(to, verify_url):
    html = f"""
    <div style="font-family:sans-serif;color:#111;max-width:480px;margin:0 auto">
      <h2>Confirm your email</h2>
      <p>Click below to confirm this address for your SupportBot account.</p>
      <p>{_button(verify_url, "Confirm email")}</p>
      <p style="color:#666;font-size:13px">This link expires in 24 hours.</p>
    </div>
    """
    return send_email(to, "Confirm your email", html)

"""Branding config — change these (or set as env vars) to reskin the bot per company."""

import os

COMPANY_NAME = os.environ.get("COMPANY_NAME", "Pizza Palace")
COMPANY_TAGLINE = os.environ.get("COMPANY_TAGLINE", "Ask me anything about our menu, timings, delivery, or returns.")
LOGO_EMOJI = os.environ.get("LOGO_EMOJI", "\U0001F355")
BRAND_COLOR = os.environ.get("BRAND_COLOR", "#2563eb")

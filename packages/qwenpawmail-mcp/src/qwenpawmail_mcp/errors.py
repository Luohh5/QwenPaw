# -*- coding: utf-8 -*-
"""User-facing error types with actionable advice
for supported mail providers."""

from __future__ import annotations


class MailError(Exception):
    """Base error for all mail operations. Message is user-readable."""


class ConfigError(MailError):
    """Configuration is missing or invalid."""


class AuthError(MailError):
    """IMAP/SMTP authentication failed (bad email or authorization code)."""


class UnsafeLoginError(AuthError):
    """NetEase rejected the login as 'Unsafe Login'
    (RFC 2971 ID not accepted)."""


class RateLimitError(MailError):
    """Temporary sending limit / throttling.

    NetEase: SMTP 451 MI:SFQ, 554 MI:STC / HL:IHU.
    Standard SMTP (e.g. QQ Mail): 421/450/452 and plain 554.
    """


class PermanentSendError(MailError):
    """Message permanently rejected.

    NetEase: SMTP 550 DT:SPM (spam-like content).
    Standard SMTP (e.g. QQ Mail): 550/554 with spam/content/reject wording.
    """


class RegistrationError(MailError):
    """Registration guidance errors, e.g. unsupported
    domain or invalid username format."""

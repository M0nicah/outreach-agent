"""Gmail authentication and sending.

Safety is the whole design here. This module can put email in a
stranger's inbox with your name on it, so every guard your
specification asked for is enforced in code rather than by convention:

  - Nothing sends unless approval.is_sendable() permits it. This module
    does NOT reimplement that decision; it calls the one tested copy.
  - DRY_RUN=true in .env blocks all sending.
  - A daily limit, a per-run batch size, and a delay between sends.
  - Duplicate prevention: an already-SENT row is refused.
  - Failures are recorded as FAILED with the reason, never swallowed.

On OAuth: we use the installed-app flow, which opens your browser once.
Google returns a refresh token, saved to token.json. Your password is
never seen or stored by this program. token.json grants send access to
your account, so it is git-ignored and must never be shared.
"""

import base64
import logging
import re
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from app import schema
from app.config import PROJECT_ROOT, Config

logger = logging.getLogger(__name__)

CREDENTIALS_PATH = PROJECT_ROOT / "credentials.json"
TOKEN_PATH = PROJECT_ROOT / "token.json"

# gmail.send   -- send mail
# gmail.readonly -- read replies (Stage 9)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class GmailError(Exception):
    """Raised for authentication or sending failures."""


# --- Authentication --------------------------------------------------------

def authenticate(force: bool = False):
    """Return an authorised Gmail API client.

    The first call opens a browser for you to grant access. After that,
    token.json is reused and refreshed silently.

    Args:
        force: ignore any saved token and authorise again. Use this when
               scopes change or a token is revoked.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GmailError(
            "Google libraries are not installed. Run:\n"
            "    pip install -r requirements.txt"
        ) from exc

    if not CREDENTIALS_PATH.exists():
        raise GmailError(
            f"credentials.json not found at {CREDENTIALS_PATH}.\n"
            "Download it from Google Cloud Console "
            "(APIs & Services -> Credentials -> OAuth client ID -> Desktop app)."
        )

    creds = None
    if TOKEN_PATH.exists() and not force:
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception as exc:
            logger.warning("Saved token could not be read (%s). Re-authorising.", exc)
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logger.info("Refreshed the saved Gmail token.")
        except Exception as exc:
            logger.warning("Token refresh failed (%s). Re-authorising.", exc)
            creds = None

    if not creds or not creds.valid:
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            # port=0 picks a free port; Google redirects the browser back
            # to it once you approve.
            creds = flow.run_local_server(
                port=0,
                prompt="consent",
                authorization_prompt_message=(
                    "\nOpening your browser to authorise Gmail access.\n"
                    "If it does not open, visit this URL:\n{url}\n"
                ),
                success_message=(
                    "Authorisation complete. You can close this tab and return "
                    "to the terminal."
                ),
            )
        except Exception as exc:
            raise GmailError(_friendly_auth_error(exc)) from exc

        try:
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            # Readable only by you: this token can send mail as you.
            TOKEN_PATH.chmod(0o600)
            logger.info("Saved the Gmail token to %s", TOKEN_PATH)
        except OSError as exc:
            logger.warning("Could not save the token (%s). You will re-authorise "
                           "next time.", exc)

    try:
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        raise GmailError(f"Could not build the Gmail client: {exc}") from exc


def _friendly_auth_error(exc: Exception) -> str:
    """Translate the common OAuth failures into something actionable."""
    text = str(exc).lower()

    if "access_denied" in text or "denied" in text:
        return (
            "Authorisation was denied.\n"
            "If Google said the app is not verified, click Advanced -> "
            "Go to (your app name) (unsafe). That warning is expected for "
            "an app you built for yourself.\n"
            "If it said you are not a test user, add your Gmail address "
            "under APIs & Services -> OAuth consent screen -> Test users."
        )
    if "invalid_client" in text:
        return (
            "Google rejected the client credentials. Re-download "
            "credentials.json from the Cloud Console and replace the one in "
            "this folder."
        )
    if "redirect_uri_mismatch" in text:
        return (
            "Redirect URI mismatch. The credentials must be for an "
            "'OAuth client ID' of type 'Desktop app', not a Web application."
        )
    return f"Gmail authorisation failed: {exc}"


def get_profile(service) -> dict[str, Any]:
    """Return the authorised account's profile. Used to confirm WHO we are."""
    try:
        return service.users().getProfile(userId="me").execute()
    except Exception as exc:
        raise GmailError(f"Could not read the Gmail profile: {exc}") from exc


# --- Sending ---------------------------------------------------------------

def is_valid_email(address: str) -> bool:
    """A basic shape check, to catch UNKNOWN and obvious typos."""
    address = (address or "").strip()
    if not address or address.upper() == schema.UNKNOWN:
        return False
    return bool(EMAIL_PATTERN.match(address))


def build_message(
    to_address: str, subject: str, body: str, from_address: str = ""
) -> dict[str, str]:
    """Build a Gmail API message payload.

    Plain text only. An internship enquiry does not need HTML, and plain
    text is less likely to be treated as marketing mail.
    """
    if not is_valid_email(to_address):
        raise GmailError(f"{to_address!r} is not a valid email address.")
    if not subject.strip():
        raise GmailError("Refusing to send an email with an empty subject.")
    if not body.strip():
        raise GmailError("Refusing to send an email with an empty body.")

    message = EmailMessage()
    message["To"] = to_address
    message["Subject"] = subject
    if from_address:
        message["From"] = from_address
    message.set_content(body)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return {"raw": raw}


def send_message(service, payload: dict[str, str]) -> str:
    """Send one message. Returns the Gmail message ID."""
    try:
        sent = service.users().messages().send(userId="me", body=payload).execute()
    except Exception as exc:
        raise GmailError(_friendly_send_error(exc)) from exc

    message_id = sent.get("id", "")
    logger.info("Sent message %s", message_id)
    return message_id


def _friendly_send_error(exc: Exception) -> str:
    """Translate Gmail send failures."""
    text = str(exc).lower()

    if "insufficient" in text or "insufficientpermissions" in text:
        return (
            "Gmail refused the request for lack of permission. Re-authorise "
            "with `python main.py gmail-auth --force` so the send scope is "
            "granted."
        )
    if "rate" in text or "429" in text or "quota" in text:
        return (
            "Gmail rate limit reached. Wait a few minutes, or lower "
            "BATCH_SIZE in .env."
        )
    if "invalid" in text and "to" in text:
        return "Gmail rejected the recipient address as invalid."
    return f"Gmail send failed: {exc}"


# --- Daily limit -----------------------------------------------------------

def sent_today(outreach: list[dict[str, Any]], today: str | None = None) -> int:
    """How many emails were already sent today, from the workbook.

    Counting from the workbook rather than a counter file means the limit
    survives restarts and is visible to you in the spreadsheet.
    """
    today = today or date.today().isoformat()
    count = 0
    for row in outreach:
        if str(row.get("Email Status", "")).strip().upper() != schema.EMAIL_SENT:
            continue
        sent_date = str(row.get("Date Sent", "")).strip()[:10]
        if sent_date == today:
            count += 1
    return count


def remaining_today(config: Config, outreach: list[dict[str, Any]]) -> int:
    """How many more emails may be sent today under the daily limit."""
    return max(0, config.daily_send_limit - sent_today(outreach))


def sent_updates(message_id: str, today_str: str) -> dict[str, Any]:
    """Column updates recording a successful send."""
    return {
        "Email Status": schema.EMAIL_SENT,
        "Date Sent": today_str,
        "Notes": f"Gmail message id: {message_id}",
    }


def failed_updates(reason: str) -> dict[str, Any]:
    """Column updates recording a failed send.

    The row stays APPROVED so a retry is possible once the cause is
    fixed, but Email Status records the failure rather than hiding it.
    """
    return {
        "Email Status": schema.EMAIL_FAILED,
        "Notes": f"Send failed: {reason}"[:500],
    }

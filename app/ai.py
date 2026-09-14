"""The AI provider layer.

Every AI call in this project goes through `get_ai_caller(config)`, which
returns a function with the signature:

    caller(config, prompt) -> raw reply text

Why this indirection exists: the rest of the codebase should not care
which company's model answers. Switching provider is then one line in
.env, not an edit across several modules. It also means tests and --mock
can inject a fake caller with no network access.

Supported providers:
    gemini  -- Google, has a free tier that needs no credit card
    openai  -- OpenAI, paid
    mock    -- fake answers for offline testing

Both real providers are asked for strict JSON output and temperature 0.
This is a classification task: the same evidence should produce the same
answer, not creative variation.
"""

import logging

from app.config import Config

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "You assess organisations as potential internship hosts. "
    "You never invent facts. You return only valid JSON."
)


class AIError(Exception):
    """Raised when an AI provider cannot be reached or is misconfigured."""


class TransientAIError(AIError):
    """A failure that is worth retrying: overload, rate limit, timeout.

    Kept separate from AIError so the caller can back off and try again
    instead of giving up. A bad API key is NOT transient -- retrying it
    just wastes time.
    """


# --- Gemini ----------------------------------------------------------------

def call_gemini(config: Config, prompt: str) -> str:
    """Send the prompt to Google Gemini and return the raw reply text.

    response_mime_type="application/json" makes the model return a bare
    JSON object rather than prose or a markdown fence, which removes the
    most common parsing failure.
    """
    try:
        from google import genai
        from google.genai import types

        # The SDK warns about "automatic function calling" on every call.
        # We do not use function calling, so the warning is pure noise.
        logging.getLogger("google_genai.models").setLevel(logging.ERROR)
    except ImportError as exc:
        raise AIError(
            "The google-genai package is not installed. Run:\n"
            "    pip install -r requirements.txt"
        ) from exc

    api_key = config.require_ai_key()
    client = genai.Client(api_key=api_key)

    try:
        response = client.models.generate_content(
            model=config.ai_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                system_instruction=SYSTEM_INSTRUCTION,
                max_output_tokens=2048,
            ),
        )
    except Exception as exc:
        error_class = TransientAIError if is_transient(exc) else AIError
        raise error_class(_friendly_error("Gemini", exc)) from exc

    text = (response.text or "").strip()
    if not text:
        # Usually means the model stopped early or a safety filter fired.
        reason = getattr(
            getattr(response, "candidates", [None])[0], "finish_reason", "unknown"
        )
        raise AIError(f"Gemini returned an empty reply (finish reason: {reason}).")
    return text


# --- OpenAI ----------------------------------------------------------------

def call_openai(config: Config, prompt: str) -> str:
    """Send the prompt to OpenAI and return the raw reply text."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise AIError(
            "The openai package is not installed. Run:\n"
            "    pip install -r requirements.txt"
        ) from exc

    client = OpenAI(api_key=config.require_ai_key())

    try:
        response = client.chat.completions.create(
            model=config.ai_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
        )
    except Exception as exc:
        error_class = TransientAIError if is_transient(exc) else AIError
        raise error_class(_friendly_error("OpenAI", exc)) from exc

    content = response.choices[0].message.content
    if not content:
        raise AIError("OpenAI returned an empty reply.")
    return content


# --- Error messages --------------------------------------------------------

# HTTP-ish signals that mean "try again shortly" rather than "you are wrong".
TRANSIENT_SIGNALS = [
    "503", "unavailable", "high demand", "overloaded",
    "429", "rate limit", "resource_exhausted",
    "500", "internal error", "timeout", "timed out", "connection",
]


def is_transient(exc: Exception) -> bool:
    """True when the error is worth retrying."""
    text = str(exc).lower()
    # A quota that is genuinely exhausted for the day is not transient,
    # but a per-minute rate limit is. We cannot always tell them apart,
    # so we retry and let the backoff sort it out.
    return any(signal in text for signal in TRANSIENT_SIGNALS)


def _friendly_error(provider: str, exc: Exception) -> str:
    """Turn a provider exception into something actionable.

    Provider SDKs raise many exception types with long messages. We match
    on the recognisable substrings and say what to DO about it.
    """
    text = str(exc)
    lowered = text.lower()

    if "api key not valid" in lowered or "api_key_invalid" in lowered or "401" in lowered:
        return (
            f"{provider} rejected the API key. Check AI_API_KEY in your .env "
            "file -- it may be mistyped, revoked, or from a different provider."
        )
    if "503" in lowered or "high demand" in lowered or "overloaded" in lowered:
        return (
            f"{provider} is temporarily overloaded (503). This is on their "
            "side and usually clears within a minute. The run already "
            "retried several times -- try again shortly."
        )
    if "quota" in lowered or "429" in lowered or "rate limit" in lowered:
        return (
            f"{provider} rate limit or quota reached. Wait a minute and try "
            "again, or use --limit to process fewer companies per run."
        )
    if "not found" in lowered and "model" in lowered:
        return (
            f"{provider} does not recognise the model '{text[:80]}'. "
            "Check AI_MODEL in your .env file."
        )
    if "connection" in lowered or "timeout" in lowered or "network" in lowered:
        return f"Could not reach {provider} -- check your internet connection."

    return f"{provider} request failed: {text}"


# --- Choosing a provider ---------------------------------------------------

PROVIDERS = {
    "gemini": call_gemini,
    "openai": call_openai,
}


def with_retries(caller, attempts: int = 4, base_delay: float = 2.0):
    """Wrap a caller so transient failures are retried with backoff.

    Delays double each time (2s, 4s, 8s). Providers routinely return 503
    "high demand" on free tiers, and a single busy moment should not
    abort a run of ten companies.

    Permanent errors -- a bad key, an unknown model -- are raised at once,
    because retrying them only wastes time.
    """
    import time

    def retrying_caller(config: Config, prompt: str) -> str:
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return caller(config, prompt)
            except TransientAIError as exc:
                last = exc
                if attempt == attempts - 1:
                    break
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "AI temporarily unavailable (attempt %d/%d). "
                    "Waiting %.0fs before retrying.",
                    attempt + 1, attempts, delay,
                )
                time.sleep(delay)
        raise last  # type: ignore[misc]

    return retrying_caller


def get_ai_caller(config: Config, use_mock: bool = False):
    """Return the caller function for the configured provider.

    Args:
        use_mock: when True, return the offline fake regardless of config.
    """
    if use_mock:
        from app.mock_ai import mock_call

        return mock_call

    provider = config.ai_provider.lower()
    caller = PROVIDERS.get(provider)
    if caller is None:
        raise AIError(
            f"Unknown AI_PROVIDER '{config.ai_provider}'. "
            f"Supported: {', '.join(sorted(PROVIDERS))}."
        )

    logger.debug("Using AI provider: %s (%s)", provider, config.ai_model)
    return with_retries(caller)

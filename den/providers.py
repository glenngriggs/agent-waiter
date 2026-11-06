"""Model providers, users' own API keys, and whose key each agent call uses.

Every agent runs one model from one provider. Its Codex process only ever
holds the agent token; the model proxy looks up the agent's provider and
picks the credentials here:

- "platform": the operator's own upstream key from the environment (the key
  in `.env`). Only accounts listed in DEN_PLATFORM_KEY_USERS may use it, and
  the list is empty by default, so a public server never spends the
  operator's key on strangers.
- "openai", "anthropic", "umgpt", "custom": the signed-in user's own key,
  added on the Settings page. Keys are encrypted at rest with a key derived
  from the server secret, are never sent back to the browser (only the last
  four characters), and never enter an agent's container.

Anthropic speaks the Messages API, not the Responses API Codex uses, so its
requests are translated by den/anthropic_bridge.py.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import OPENAI_BASE_URL, TOOLKIT_BASE_URL, Settings

logger = logging.getLogger("den.providers")

PLATFORM = "platform"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
MAX_KEY_LEN = 400
MAX_EXTRA_MODELS = 12
MAX_MODEL_ID_LEN = 100
VERIFY_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    note: str = ""


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    api: str  # "responses" (passed through) or "anthropic" (translated)
    default_base_url: str | None
    key_url: str | None
    key_placeholder: str
    description: str
    models: tuple[ModelInfo, ...] = ()
    custom_url: bool = False  # the user supplies the base URL and model ids


_COURSE_MODELS = (
    ModelInfo("gpt-6.1-sol", "GPT-6.1 Sol", "Strongest"),
    ModelInfo("gpt-6-luna", "GPT-6 Luna", "Fast, low cost"),
)

CATALOG: dict[str, Provider] = {
    "anthropic": Provider(
        id="anthropic",
        label="Anthropic",
        api="anthropic",
        default_base_url=ANTHROPIC_BASE_URL,
        key_url="https://console.anthropic.com/settings/keys",
        key_placeholder="sk-ant-…",
        description="Claude models, with your Anthropic API key.",
        models=(
            ModelInfo("claude-opus-5-5", "Claude Opus 5.5", "Most capable for everyday work"),
            ModelInfo("claude-sonnet-5-5", "Claude Sonnet 5.5", "Fast and capable"),
            ModelInfo("claude-haiku-5-5", "Claude Haiku 5.5", "Fastest, lowest cost"),
            ModelInfo("claude-fable-5-1", "Claude Fable 5.1", "Anthropic's most capable, highest cost"),
        ),
    ),
    "openai": Provider(
        id="openai",
        label="OpenAI",
        api="responses",
        default_base_url=OPENAI_BASE_URL,
        key_url="https://platform.openai.com/api-keys",
        key_placeholder="sk-…",
        description="GPT models, with your OpenAI API key.",
        models=_COURSE_MODELS,
    ),
    "umgpt": Provider(
        id="umgpt",
        label="U-M GPT Toolkit",
        api="responses",
        default_base_url=TOOLKIT_BASE_URL,
        key_url="https://its.umich.edu/computing/ai/gpt-toolkit",
        key_placeholder="Toolkit API key",
        description="University of Michigan GPT Toolkit key.",
        models=_COURSE_MODELS,
    ),
    "custom": Provider(
        id="custom",
        label="OpenAI-compatible",
        api="responses",
        default_base_url=None,
        key_url=None,
        key_placeholder="API key",
        description="Any server that implements the OpenAI Responses API "
        "(a gateway, Azure OpenAI, or a self-hosted model).",
        custom_url=True,
    ),
}


class ProviderInputError(Exception):
    """A key or URL a user entered was refused; the message is for the user."""


@dataclass(frozen=True)
class Credentials:
    provider: str
    api: str
    base_url: str
    api_key: str = field(repr=False)
    source: str  # "own" or "platform"


# --------------------------------------------------------------------------
# Catalog lookups
# --------------------------------------------------------------------------


def user_providers(settings: Settings) -> list[Provider]:
    """Providers a user can add their own key for, in display order.

    None in chatgpt mode: there every agent uses the operator's mounted login.
    """
    if settings.model_auth == "chatgpt":
        return []
    return [
        p for p in CATALOG.values() if settings.allow_custom_provider or not p.custom_url
    ]


def provider_label(settings: Settings, provider_id: str) -> str:
    if provider_id == PLATFORM:
        return settings.platform_label
    provider = CATALOG.get(provider_id)
    return provider.label if provider else provider_id


def _split_models(raw: str | None) -> list[str]:
    return [m for m in (raw or "").split(",") if m]


def clean_model_list(raw: str | list[str] | None) -> list[str]:
    """Validate a user's comma-separated extra model ids; raise ProviderInputError."""
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    models: list[str] = []
    for item in items:
        model = str(item).strip()
        if not model:
            continue
        if len(model) > MAX_MODEL_ID_LEN or any(c.isspace() or c == "," for c in model):
            raise ProviderInputError(f"'{model[:40]}' is not a valid model id.")
        if model not in models:
            models.append(model)
    if len(models) > MAX_EXTRA_MODELS:
        raise ProviderInputError(f"List at most {MAX_EXTRA_MODELS} models.")
    return models


def _key_row(ctx: Any, user_id: str, provider_id: str) -> dict | None:
    return ctx.db.one(
        "SELECT * FROM api_keys WHERE user_id = ? AND provider = ?", (user_id, provider_id)
    )


def models_for(ctx: Any, user_id: str, provider_id: str) -> list[ModelInfo]:
    """Models this user may pick from this provider (whether or not a key is set)."""
    settings = ctx.settings
    if provider_id == PLATFORM:
        return [ModelInfo(m, settings.label_for(m)) for m in settings.models]
    provider = CATALOG.get(provider_id)
    if provider is None or (provider.custom_url and not settings.allow_custom_provider):
        return []
    models = list(provider.models)
    known = {m.id for m in models}
    extra = list(settings.provider_extra_models.get(provider_id, ()))
    row = _key_row(ctx, user_id, provider_id)
    if row is not None:
        extra += _split_models(row["models"])
    for model in extra:
        if model not in known:
            known.add(model)
            models.append(ModelInfo(model, model, "Added by you" if row else ""))
    return models


def model_offered(ctx: Any, user_id: str, provider_id: str, model: str) -> bool:
    return any(m.id == model for m in models_for(ctx, user_id, provider_id))


def user_email(ctx: Any, user_id: str) -> str:
    row = ctx.db.one("SELECT email FROM users WHERE id = ?", (user_id,))
    return row["email"] if row else ""


def platform_available(ctx: Any, user_id: str) -> bool:
    return ctx.settings.platform_allowed(user_email(ctx, user_id))


# --------------------------------------------------------------------------
# Encryption at rest
# --------------------------------------------------------------------------


def _fernet(secret: bytes) -> Fernet:
    # A dedicated subkey, so the session/agent-token secret itself never
    # doubles as an encryption key.
    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"den user api keys v1"
    ).derive(secret)
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_key(secret: bytes, api_key: str) -> str:
    return _fernet(secret).encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt_key(secret: bytes, ciphertext: str) -> str | None:
    """The stored key, or None when it cannot be decrypted (secret rotated)."""
    try:
        return _fernet(secret).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def key_hint(api_key: str) -> str:
    """What the browser may see of a key: its last four characters."""
    return "…" + api_key[-4:] if len(api_key) >= 12 else "…"


# --------------------------------------------------------------------------
# Choosing credentials
# --------------------------------------------------------------------------


def base_url_for(settings: Settings, provider_id: str, row: dict | None) -> str | None:
    provider = CATALOG[provider_id]
    if provider.custom_url:
        return (row or {}).get("base_url") or None
    return settings.provider_base_urls.get(provider_id) or provider.default_base_url


def resolve_credentials(ctx: Any, user_id: str, provider_id: str) -> Credentials | None:
    """The key a call for this user and provider should use, or None."""
    settings = ctx.settings
    if provider_id == PLATFORM:
        if settings.model_auth == "chatgpt" and platform_available(ctx, user_id):
            # Codex uses the mounted login directly; there is no key to hand out.
            return Credentials(PLATFORM, "chatgpt", "", "", "platform")
        if settings.upstream_api_key and platform_available(ctx, user_id):
            return Credentials(
                PLATFORM, "responses", settings.upstream_base_url, settings.upstream_api_key,
                "platform",
            )
        return None
    provider = CATALOG.get(provider_id)
    if provider is None:
        return None
    row = _key_row(ctx, user_id, provider_id)
    if row is None:
        return None
    api_key = decrypt_key(ctx.secret, row["key_ciphertext"])
    base_url = base_url_for(settings, provider_id, row)
    if not api_key or not base_url:
        return None
    return Credentials(provider_id, provider.api, base_url.rstrip("/"), api_key, "own")


def no_key_message(settings: Settings, provider_id: str) -> str:
    if provider_id == PLATFORM:
        return (
            "This agent uses the server's shared key, which your account may not use. "
            "Add your own API key in Settings, then switch the agent to one of its models."
        )
    return (
        f"This agent runs on {provider_label(settings, provider_id)}, and there is no "
        "API key for it. Add one in Settings → API keys."
    )


def rejected_key_message(settings: Settings, provider_id: str, source: str | None) -> str:
    if source == "platform" or provider_id == PLATFORM:
        return (
            "The model provider rejected the server's shared key. "
            "The operator needs to check it."
        )
    return (
        f"{provider_label(settings, provider_id)} rejected your API key. "
        "Check or replace it in Settings → API keys."
    )


# --------------------------------------------------------------------------
# Checking what users enter
# --------------------------------------------------------------------------


def _is_private(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not ip.is_global or ip.is_multicast


async def check_base_url(url: str, allow_private: bool) -> str:
    """Normalize a user-supplied base URL; raise ProviderInputError when it is refused."""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ProviderInputError("Enter a full URL such as https://example.com/v1.")
    if parts.username or parts.password or parts.fragment or parts.query:
        raise ProviderInputError("The URL may not contain a login, a query or a fragment.")
    if len(url) > 300:
        raise ProviderInputError("The URL is too long.")
    if allow_private:
        return url
    if parts.scheme != "https":
        raise ProviderInputError("The URL must use https.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or 443, type=socket.SOCK_STREAM
        )
    except OSError:
        raise ProviderInputError(f"Could not find the server {parts.hostname}.") from None
    if any(_is_private(info[4][0]) for info in infos):
        raise ProviderInputError("That address is on a private network, which this server does not allow.")
    return url


def check_api_key(raw: str) -> str:
    key = raw.strip()
    if not key:
        raise ProviderInputError("Paste the API key.")
    if len(key) > MAX_KEY_LEN or any(c.isspace() for c in key) or not key.isprintable():
        raise ProviderInputError("That does not look like an API key.")
    return key


async def verify_key(ctx: Any, provider_id: str, api_key: str, base_url: str) -> str | None:
    """Ask the provider whether it accepts the key, at no cost.

    Returns None when the key works, else a message for the user. Network
    trouble counts as a failure, because a key that cannot be checked
    usually means a mistyped URL.
    """
    provider = CATALOG[provider_id]
    if provider.api == "anthropic":
        from . import anthropic_bridge  # local: the bridge imports this module

        return await anthropic_bridge.verify_key(api_key, base_url)
    if ctx.http is None:
        return "The server is still starting. Try again in a moment."
    # An empty Responses request is free: a good key gets a 400-style
    # complaint about the missing model, a bad one gets 401. (Some gateways,
    # the Toolkit among them, answer GET /models with 200 for any key.)
    try:
        response = await ctx.http.post(
            f"{base_url}/responses",
            json={},
            headers={"authorization": f"Bearer {api_key}"},
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return f"Could not reach {base_url} ({type(exc).__name__})."
    if response.status_code in (401, 403):
        return f"{provider.label} rejected this key."
    if response.status_code == 404:
        return f"There is no Responses API at {base_url}. Check the URL (it usually ends in /v1)."
    return None

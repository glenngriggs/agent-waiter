"""Settings routes: the user's model providers and API keys, and token usage.

Everything here belongs to the signed-in user only. A stored key is never
returned: the browser gets its last four characters, when it was checked,
and the models it unlocks.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from . import providers, usage
from .auth import current_user
from .context import AppContext, get_ctx
from .db import iso, utcnow

logger = logging.getLogger("den.account")

router = APIRouter(prefix="/api")


class KeyBody(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    models: str | list[str] | None = None


def _models_out(models: list[providers.ModelInfo]) -> list[dict]:
    return [{"id": m.id, "label": m.label, "note": m.note} for m in models]


def _agents_using(ctx: AppContext, user_id: str, provider_id: str) -> int:
    return ctx.db.one(
        "SELECT COUNT(*) AS n FROM agents WHERE user_id = ? AND provider = ?", (user_id, provider_id)
    )["n"]


def _stranded_agents(
    ctx: AppContext, user_id: str, provider: providers.Provider, new_models: list[str]
) -> list[dict]:
    """This user's agents on a model that saving `new_models` would stop offering.

    An agent whose model disappears still passes the key check, so its turns
    would start Codex only to fail with a misleading "try again". Agents
    already on a model that is not offered (the operator dropped it) do not
    count, so they never block saving a key.
    """
    offered_now = {m.id for m in providers.models_for(ctx, user_id, provider.id)}
    offered_after = (
        {m.id for m in provider.models}
        | set(ctx.settings.provider_extra_models.get(provider.id, ()))
        | set(new_models)
    )
    rows = ctx.db.all(
        "SELECT name, model FROM agents WHERE user_id = ? AND provider = ? ORDER BY created_at",
        (user_id, provider.id),
    )
    return [r for r in rows if r["model"] in offered_now and r["model"] not in offered_after]


def provider_out(ctx: AppContext, user: dict, provider_id: str) -> dict:
    user_id = user["id"]
    agents = _agents_using(ctx, user_id, provider_id)
    if provider_id == providers.PLATFORM:
        available = providers.resolve_credentials(ctx, user_id, provider_id) is not None
        return {
            "id": providers.PLATFORM,
            "label": ctx.settings.platform_label,
            "description": "Provided by the people who run this server. Your own keys are not needed for these models.",
            "api": "responses",
            "user_key": False,
            "custom_url": False,
            "key_url": None,
            "key_placeholder": "",
            "key": None,
            "available": available,
            "source": "platform" if available else None,
            "agents": agents,
            "models": _models_out(providers.models_for(ctx, user_id, provider_id)),
        }
    provider = providers.CATALOG[provider_id]
    row = ctx.db.one(
        "SELECT key_hint, base_url, models, verified_at, updated_at FROM api_keys "
        "WHERE user_id = ? AND provider = ?",
        (user_id, provider_id),
    )
    usable = row is not None and providers.resolve_credentials(ctx, user_id, provider_id) is not None
    return {
        "id": provider.id,
        "label": provider.label,
        "description": provider.description,
        "api": provider.api,
        "user_key": True,
        "custom_url": provider.custom_url,
        "key_url": provider.key_url,
        "key_placeholder": provider.key_placeholder,
        "key": None if row is None else {
            "hint": row["key_hint"],
            "base_url": row["base_url"],
            "models": [m for m in (row["models"] or "").split(",") if m],
            "verified_at": row["verified_at"],
            "updated_at": row["updated_at"],
            # False when the key can no longer be decrypted (server secret changed).
            "usable": usable,
        },
        "available": usable,
        "source": "own" if usable else None,
        "agents": agents,
        "models": _models_out(providers.models_for(ctx, user_id, provider_id)),
    }


def _user_provider(ctx: AppContext, provider_id: str) -> providers.Provider:
    provider = providers.CATALOG.get(provider_id)
    if provider is None or provider not in providers.user_providers(ctx.settings):
        raise HTTPException(404, "Unknown provider.")
    return provider


@router.get("/providers")
async def list_providers(
    user: dict = Depends(current_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    out = [provider_out(ctx, user, p.id) for p in providers.user_providers(ctx.settings)]
    platform = provider_out(ctx, user, providers.PLATFORM)
    # The shared provider is listed when the user may use it, or still has
    # agents on it (so they can see why those agents stopped working).
    if platform["available"] or platform["agents"]:
        out.insert(0, platform)
    return {"providers": out}


@router.put("/providers/{provider_id}/key")
async def save_key(
    provider_id: str,
    body: KeyBody,
    user: dict = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    provider = _user_provider(ctx, provider_id)
    existing = ctx.db.one(
        "SELECT * FROM api_keys WHERE user_id = ? AND provider = ?", (user["id"], provider_id)
    )
    try:
        typed_key = body.api_key is not None and bool(body.api_key.strip())
        if typed_key:
            api_key = providers.check_api_key(body.api_key)
        elif existing is not None:
            api_key = providers.decrypt_key(ctx.secret, existing["key_ciphertext"]) or ""
            if not api_key:
                raise providers.ProviderInputError("Paste the key again; the saved one can no longer be read.")
        else:
            raise providers.ProviderInputError("Paste the API key.")

        if provider.custom_url:
            raw_url = body.base_url if body.base_url is not None else (existing or {}).get("base_url")
            if not raw_url:
                raise providers.ProviderInputError("Enter the server's base URL.")
            base_url = await providers.check_base_url(raw_url, ctx.settings.allow_private_provider_urls)
            # verify_key sends the key to this URL. A saved key may only go
            # back to the server it was saved for, or anyone holding the
            # session could point it at their own server and read it.
            if not typed_key and base_url != (existing or {}).get("base_url"):
                raise providers.ProviderInputError("Paste the key again to change the server URL.")
        else:
            base_url = providers.base_url_for(ctx.settings, provider_id, None)

        if body.models is not None:
            models = providers.clean_model_list(body.models)
        else:
            models = [m for m in ((existing or {}).get("models") or "").split(",") if m]
        if provider.custom_url and not models:
            raise providers.ProviderInputError("List at least one model id this server offers.")
    except providers.ProviderInputError as exc:
        raise HTTPException(400, str(exc)) from None

    stranded = _stranded_agents(ctx, user["id"], provider, models)
    if stranded:
        names = ", ".join(f"'{r['name']}' ({r['model']})" for r in stranded)
        raise HTTPException(
            400,
            f"These agents still run on a model you are removing: {names}. "
            "Switch them to another model first, then save the key.",
        )

    problem = await providers.verify_key(ctx, provider_id, api_key, base_url)
    if problem is not None:
        raise HTTPException(400, problem)

    now = iso(utcnow())
    ctx.db.execute(
        "INSERT INTO api_keys (user_id, provider, key_ciphertext, key_hint, base_url, models, "
        "verified_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (user_id, provider) DO UPDATE SET key_ciphertext = excluded.key_ciphertext, "
        "key_hint = excluded.key_hint, base_url = excluded.base_url, models = excluded.models, "
        "verified_at = excluded.verified_at, updated_at = excluded.updated_at",
        (
            user["id"], provider_id, providers.encrypt_key(ctx.secret, api_key),
            providers.key_hint(api_key), base_url if provider.custom_url else None,
            ",".join(models), now, now, now,
        ),
    )
    logger.info("user %s saved a %s key", user["id"], provider_id)
    return {"provider": provider_out(ctx, user, provider_id)}


@router.post("/providers/{provider_id}/key/test")
async def test_key(
    provider_id: str, user: dict = Depends(current_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    provider = _user_provider(ctx, provider_id)
    creds = providers.resolve_credentials(ctx, user["id"], provider_id)
    if creds is None:
        raise HTTPException(404, "There is no usable key for this provider.")
    if provider.custom_url:
        # The name may point somewhere else now than when the key was saved.
        try:
            await providers.check_base_url(creds.base_url, ctx.settings.allow_private_provider_urls)
        except providers.ProviderInputError as exc:
            return {"ok": False, "message": str(exc),
                    "provider": provider_out(ctx, user, provider_id)}
    problem = await providers.verify_key(ctx, provider_id, creds.api_key, creds.base_url)
    if problem is None:
        ctx.db.execute(
            "UPDATE api_keys SET verified_at = ? WHERE user_id = ? AND provider = ?",
            (iso(utcnow()), user["id"], provider_id),
        )
    return {"ok": problem is None, "message": problem or "The key works.",
            "provider": provider_out(ctx, user, provider_id)}


@router.delete("/providers/{provider_id}/key", status_code=204)
async def delete_key(
    provider_id: str, user: dict = Depends(current_user), ctx: AppContext = Depends(get_ctx)
) -> Response:
    _user_provider(ctx, provider_id)
    cur = ctx.db.execute(
        "DELETE FROM api_keys WHERE user_id = ? AND provider = ?", (user["id"], provider_id)
    )
    if not cur.rowcount:
        raise HTTPException(404, "There is no key for this provider.")
    logger.info("user %s removed their %s key", user["id"], provider_id)
    return Response(status_code=204)


@router.get("/usage")
async def get_usage(
    days: int = 30, user: dict = Depends(current_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    days = max(1, min(int(days), 365))
    result = usage.summary(ctx, user["id"], days)
    for row in result["by_model"]:
        row["provider_label"] = providers.provider_label(ctx.settings, row["provider"])
    return result

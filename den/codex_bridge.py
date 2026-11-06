"""The Codex connection: runs one turn of an agent through Codex app server.

Every turn (chat message, job run or onboarding probe) gets its own
`AsyncCodex` client, which launches its own `codex app-server` process
through the runtime (``docker exec -i`` in production). That is what lets
Stop kill a running command by closing the client without touching a job
running in the same container (design doc, "Alternatives").

A `TurnRunner` translates the SDK's notification stream into platform
events (4.1 "Event shape" and the item table), keeps the partial text of a
streaming reply for browsers that connect mid-turn, and writes the turn's
outcome to the `turns` row. `TurnRegistry` is the in-memory index of running
turns used for "is the agent busy?", Stop, delete and shutdown.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
from contextlib import aclosing
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    CodexError,
    JsonRpcError,
    Sandbox,
    TransportClosedError,
    is_retryable_error,
)
from pydantic import RootModel

from .agent_files import (
    agent_paths,
    developer_instructions,
    prepare_scratch_home,
    remove_scratch_home,
)
from .db import iso, new_id, utcnow
from .runtime.base import AgentRuntimeError

if TYPE_CHECKING:  # type hints only; context imports this module lazily too
    from openai_codex import AsyncThread, AsyncTurnHandle
    from openai_codex.generated.v2_all import TurnError

    from .context import AppContext

logger = logging.getLogger("den.codex_bridge")

# The onboarding check turn. The fake model in tests answers "ready" to it.
PROBE_PROMPT = "Reply with the single word: ready"

# Field limits from the 4.1 item table, so persisted events stay small.
COMMAND_OUTPUT_LIMIT = 8000
MCP_ARGUMENTS_LIMIT = 2000
MCP_RESULT_LIMIT = 4000
OTHER_ERROR_LIMIT = 1000

INTERRUPT_TIMEOUT_SECONDS = 5.0
# How long stop() waits for run() to finish writing its outcome, so callers
# (the Stop route, delete, restart) see a free agent when stop() returns.
STOP_WAIT_SECONDS = 10.0

# User-facing failure messages (Technical Plan, "What the user sees when
# something fails"). `{model}` is filled in with the agent's model id.
MESSAGES = {
    "unauthorized": (
        "The model provider rejected the platform's credentials. "
        "The operator needs to check the API key."
    ),
    "rate_limited": (
        "The model is rate-limited right now. Wait a minute or switch this agent to another model."
    ),
    "model_unavailable": (
        "The model {model} is not answering right now. "
        "Try again, or switch this agent to another model."
    ),
    "context_full": "This conversation is too long for the model. Start a new conversation.",
    "runtime_crashed": (
        "The agent's runtime stopped unexpectedly. "
        "Your files and conversation are safe; send the message again."
    ),
    "interrupted": "Stopped.",
    "platform_restart": "The platform restarted while this was running.",
    "timeout": "The turn took too long and was stopped.",
}

# TurnError.codex_error_info value -> platform error kind (4.1 "Error kinds").
_ERROR_INFO_KINDS = {
    "unauthorized": "unauthorized",
    "usageLimitExceeded": "rate_limited",
    "rateLimitExceeded": "rate_limited",
    "sessionBudgetExceeded": "rate_limited",
    "serverOverloaded": "model_unavailable",
    "internalServerError": "model_unavailable",
    "badRequest": "model_unavailable",
    "flexUnavailable": "model_unavailable",
    "contextWindowExceeded": "context_full",
}

RESUME_FAILED_NOTICE = "The previous conversation could not be restored, so a new one was started."


class Busy(Exception):
    """A chat turn is already running for this agent."""


@dataclass
class TurnOutcome:
    status: str  # completed / failed / interrupted
    final_response: str | None
    error_kind: str | None
    error_message: str | None
    thread_id: str | None


class _StopRequested(Exception):
    """Internal: stop() was called before the turn reached the stream."""


# --------------------------------------------------------------------------
# Item translation
# --------------------------------------------------------------------------


def _value(x: Any) -> Any:
    """SDK enums and root models -> plain JSON values (4.1: send `.value`)."""
    if isinstance(x, Enum):
        return x.value
    if isinstance(x, RootModel):
        return _value(x.root)
    return x


def _field(obj: Any, name: str) -> Any:
    """Read a field from either a pydantic model or a plain dict.

    MCP result content blocks arrive as untyped dicts, while most of the
    SDK's payloads are models, so helpers below accept both.
    """
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _clip_tail(text: str | None, limit: int) -> str:
    text = text or ""
    return text[-limit:] if len(text) > limit else text


def _clip_head(text: str | None, limit: int) -> str:
    text = text or ""
    return text[:limit] if len(text) > limit else text


def _change_kind(kind: Any) -> str | None:
    # PatchChangeKind is a root model over {type: "add"|"delete"|"update"}.
    inner = kind.root if isinstance(kind, RootModel) else kind
    return _value(_field(inner, "type"))


def _mcp_result_text(result: Any) -> str:
    if result is None:
        return ""
    parts = []
    for block in _field(result, "content") or []:
        if _field(block, "type") == "text" and _field(block, "text") is not None:
            parts.append(str(_field(block, "text")))
    return _clip_head("\n".join(parts), MCP_RESULT_LIMIT)


def _json_text(value: Any, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return _clip_head(text, limit)


def translate_item(item: Any) -> dict | None:
    """Translate an SDK thread item into the platform item shape (4.1 table).

    Accepts a `ThreadItem` root model or the item inside it. Returns None for
    items the platform does not show (the user's own message is already in
    `turn_started`).
    """
    if isinstance(item, RootModel):
        item = item.root
    item_type = _value(_field(item, "type"))
    data: dict[str, Any] = {"item_id": _field(item, "id")}

    if item_type == "userMessage":
        return None
    if item_type == "agentMessage":
        data.update(item_type="agent_message", text=item.text or "")
    elif item_type == "reasoning":
        data.update(item_type="reasoning", text="\n".join(item.summary or []))
    elif item_type == "commandExecution":
        data.update(
            item_type="command",
            command=item.command,
            status=_value(item.status),
            exit_code=item.exit_code,
            output=_clip_tail(item.aggregated_output, COMMAND_OUTPUT_LIMIT),
            duration_ms=item.duration_ms,
        )
    elif item_type == "fileChange":
        data.update(
            item_type="file_change",
            status=_value(item.status),
            changes=[{"path": c.path, "kind": _change_kind(c.kind)} for c in item.changes or []],
        )
    elif item_type == "mcpToolCall":
        data.update(
            item_type="mcp_tool_call",
            server=item.server,
            tool=item.tool,
            status=_value(item.status),
            arguments=_json_text(item.arguments, MCP_ARGUMENTS_LIMIT),
            result_text=_mcp_result_text(item.result),
            error=item.error.message if item.error is not None else None,
        )
    elif item_type == "webSearch":
        data.update(item_type="web_search", query=item.query)
    else:
        data.update(item_type="other", raw_type=item_type)
    return data


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------


def _error_info(info: Any) -> tuple[str | None, int | None]:
    """Return (name, http_status) from a TurnError.codex_error_info.

    The info is either a plain value such as "unauthorized", or a one-key
    object such as {"responseStreamConnectionFailed": {"httpStatusCode": 401}}.
    """
    if info is None:
        return None, None
    info = info.root if isinstance(info, RootModel) else info
    if isinstance(info, (str, Enum)):
        return str(_value(info)), None
    if isinstance(info, dict):
        items = list(info.items())
    else:
        # A typed model with exactly one field; report it by its wire alias.
        items = []
        for name, field in type(info).model_fields.items():
            value = getattr(info, name, None)
            if value is not None:
                items.append((field.alias or name, value))
    if not items:
        return None, None
    name, inner = items[0]
    status = _field(inner, "http_status_code")
    if status is None:
        status = _field(inner, "httpStatusCode")
    return name, status if isinstance(status, int) else None


def _kind_for_http_status(status: int | None) -> str | None:
    # Codex reports some provider failures only as an HTTP status inside a
    # structured info (for example after its retries run out). Map the common
    # ones so the user still gets the cause-specific message.
    if status is None:
        return None
    if status == 401:
        return "unauthorized"
    if status == 429:
        return "rate_limited"
    if status in (403, 404) or status >= 500:
        # 403 here is our own proxy refusing a model it does not offer.
        return "model_unavailable"
    return None


def classify_turn_error(error: "TurnError | None", model: str) -> tuple[str, str]:
    """(error_kind, user message) for a failed turn's TurnError."""
    if error is None:
        return "other", "The model run failed without saying why. Try again."
    name, http_status = _error_info(error.codex_error_info)
    kind = _ERROR_INFO_KINDS.get(name or "") or _kind_for_http_status(http_status)
    if kind is not None:
        return kind, MESSAGES[kind].format(model=model)
    detail = error.message or "unknown error"
    if error.additional_details:
        detail = f"{detail} ({error.additional_details})"
    return "other", _clip_head(f"The model run failed: {detail}", OTHER_ERROR_LIMIT)


def classify_exception(exc: BaseException, model: str) -> tuple[str, str]:
    """(error_kind, user message) for an exception raised while running a turn."""
    if isinstance(exc, AgentRuntimeError):
        return "container_failed", f"The agent's container could not start: {exc.message}"
    if isinstance(exc, TransportClosedError):
        return "runtime_crashed", MESSAGES["runtime_crashed"]
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout", MESSAGES["timeout"]
    if isinstance(exc, JsonRpcError):
        if is_retryable_error(exc):
            return "model_unavailable", MESSAGES["model_unavailable"].format(model=model)
        return "other", _clip_head(
            f"The agent's runtime refused the request: {exc.message}", OTHER_ERROR_LIMIT
        )
    if isinstance(exc, OSError):
        # The launcher itself (docker or the codex binary) could not be run.
        return "runtime_crashed", MESSAGES["runtime_crashed"]
    return "other", _clip_head(
        f"Something went wrong while running the agent: {type(exc).__name__}: {exc}",
        OTHER_ERROR_LIMIT,
    )


# --------------------------------------------------------------------------
# TurnRunner
# --------------------------------------------------------------------------


def _thread_is_gone(exc: BaseException) -> bool:
    """True when thread_resume failed because the saved thread no longer exists.

    Codex 0.163 answers `no rollout found for thread id <id>` (JSON-RPC
    -32600) when CODEX_HOME has no record of the thread, and `invalid session
    id` for an id it cannot parse; both mean there is nothing to resume.
    """
    text = str(exc).lower()
    return "no rollout found" in text or "invalid session id" in text


class TurnRunner:
    """Runs exactly one turn and owns its Codex client from start to finish."""

    def __init__(
        self,
        ctx: "AppContext",
        *,
        agent: dict,
        turn_id: str,
        prompt: str,
        source: str,
        channel: str,
        thread_id: str | None,
        developer_instructions: str,
        ephemeral: bool = False,
        on_thread_started: Callable[[str], Any] | None = None,
        scratch_home: bool = False,
    ) -> None:
        self.ctx = ctx
        self.agent = agent
        self.agent_id: str = agent["id"]
        self.turn_id = turn_id
        self.prompt = prompt
        self.source = source
        self.channel = channel
        self.resume_thread_id = thread_id
        self.developer_instructions = developer_instructions
        self.ephemeral = ephemeral
        self.on_thread_started = on_thread_started
        # Job runs and the probe get their own CODEX_HOME so they can run while
        # a chat turn holds the agent's main one (see AgentRuntime.codex_config).
        self.scratch_home = scratch_home
        self._scratch_subdir: str | None = None

        self.thread_id: str | None = None  # the Codex thread actually used
        self.codex_turn_id: str | None = None
        self.final_response: str | None = None
        # item_id -> text streamed so far, for snapshot() while a reply streams.
        self.partial: dict[str, str] = {}

        self.stop_requested = False
        self.stop_reason: str | None = None
        self.stop_message: str | None = None
        self.outcome: TurnOutcome | None = None

        self._codex: AsyncCodex | None = None
        self._turn: "AsyncTurnHandle | None" = None
        self._turn_error: "TurnError | None" = None  # last non-retried `error` event
        self._retry_notice_sent = False
        self._result: tuple[str, str | None, str | None] | None = None
        self._finished = asyncio.Event()

    # -- public API --------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    def snapshot(self) -> dict:
        return {"turn_id": self.turn_id, "items": dict(self.partial)}

    async def run(self) -> TurnOutcome:
        """Run the turn to the end; never raises except for task cancellation."""
        # Overwritten by every branch below; it only survives if something
        # outside them (KeyboardInterrupt, SystemExit) unwinds the turn.
        result: tuple[str, str | None, str | None] = (
            "failed", "other", "The turn ended unexpectedly.",
        )
        try:
            result = await self._drive()
        except asyncio.CancelledError:
            # Cancelled from outside (for example asyncio.wait_for timing out).
            # Record the turn as stopped, then let the cancellation propagate
            # so the caller's timeout logic still sees it.
            result = self._stopped_result()
            raise
        except _StopRequested:
            result = self._stopped_result()
        except AgentRuntimeError as exc:
            logger.warning("turn %s: container failed: %s %s", self.turn_id, exc.message, exc.detail)
            result = ("failed", *classify_exception(exc, self._model))
            self._emit_container_failed(exc)
        except (CodexError, OSError) as exc:
            if self.stop_requested:
                # Closing the client is how Stop works, so the transport
                # error it causes is the expected end of a stopped turn.
                result = self._stopped_result()
            else:
                logger.warning("turn %s: codex failed: %r", self.turn_id, exc)
                result = ("failed", *classify_exception(exc, self._model))
        except Exception as exc:  # a bug in our code or the SDK: log, show, don't hang
            logger.exception("turn %s crashed", self.turn_id)
            result = ("failed", *classify_exception(exc, self._model))
        finally:
            await self._finish(result)
        assert self.outcome is not None
        return self.outcome

    async def stop(self, reason: str = "user", *, message: str | None = None) -> None:
        """Interrupt the turn, close the client, and wait for run() to finish.

        `reason` is "user" (Stop, delete, restart, cancel), "timeout" (the
        turn then ends `failed` with kind `timeout` and `message`) or
        "shutdown" (ends `interrupted` with kind `platform_restart`). Safe to
        call before the turn has started and more than once.
        """
        if not self.stop_requested:
            self.stop_requested = True
            self.stop_reason = reason
            self.stop_message = message
        codex, turn = self._codex, self._turn
        if codex is not None and turn is not None:
            # Ask Codex to end the turn cleanly first. The docs show a running
            # command keeps going after interrupt until the client closes, so
            # the close below is what actually ends it.
            try:
                await asyncio.wait_for(turn.interrupt(), INTERRUPT_TIMEOUT_SECONDS)
            except (asyncio.TimeoutError, CodexError, OSError) as exc:
                logger.info("turn %s: interrupt failed (%r); closing the client", self.turn_id, exc)
        await self._close_client()
        try:
            await asyncio.wait_for(self._finished.wait(), STOP_WAIT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("turn %s: still finishing %ss after stop", self.turn_id, STOP_WAIT_SECONDS)

    # -- the turn ----------------------------------------------------------

    @property
    def _model(self) -> str:
        return str(self.agent.get("model") or "")

    def _check_stop(self) -> None:
        # Called right before every SDK call: after stop() closes the client,
        # AsyncCodex would lazily launch a *new* app-server on the next call.
        if self.stop_requested:
            raise _StopRequested()

    async def _drive(self) -> tuple[str, str | None, str | None]:
        ctx = self.ctx
        # Re-read the agent: its model or token version may have changed since
        # the runner was built (PATCH, Restart).
        self.agent = ctx.get_agent(self.agent_id) or self.agent
        agent = self.agent
        self._check_stop()

        await self._ensure_container(agent)
        self._check_stop()

        if self.scratch_home:
            self._scratch_subdir = prepare_scratch_home(
                ctx.settings, agent_paths(ctx.settings, agent["id"]), agent["model"], self.turn_id
            )
        codex = AsyncCodex(ctx.runtime.codex_config(agent, self._scratch_subdir))
        self._codex = codex
        await codex.__aenter__()  # launches app-server and runs `initialize`
        self._check_stop()

        thread = await self._open_thread(codex, agent, ctx.runtime.workspace_cwd(agent))
        self.thread_id = thread.id
        self._check_stop()

        turn = await thread.turn(self.prompt)
        self._turn = turn
        self.codex_turn_id = turn.id
        self._save_codex_ids()
        # stop() may have run while turn/start was in flight (it then had no
        # handle to interrupt and only closed the client).
        self._check_stop()

        status: str | None = None
        turn_error: "TurnError | None" = None
        async with aclosing(turn.stream()) as stream:
            async for ev in stream:
                done = self._handle_event(ev.method, ev.payload)
                if done is not None:
                    status, turn_error = done

        if status is None:
            # The stream ended without turn/completed: the process went away.
            if self.stop_requested:
                return self._stopped_result()
            return ("failed", *classify_exception(TransportClosedError("stream ended"), self._model))
        if status == "completed":
            return ("completed", None, None)
        if status == "interrupted":
            return self._stopped_result()
        error = turn_error or self._turn_error
        # The raw error (with codex_error_info) is what an operator needs to
        # tell a bad key from a provider outage; the user gets the mapped text.
        logger.warning("turn %s failed (status %s): %r", self.turn_id, status, error)
        return ("failed", *classify_turn_error(error, self._model))

    async def _ensure_container(self, agent: dict) -> None:
        runtime = self.ctx.runtime
        try:
            before = await runtime.status(self.agent_id)
        except AgentRuntimeError as exc:
            logger.info("turn %s: status check failed: %s", self.turn_id, exc.message)
            before = "unknown"
        if before != "running":
            self.ctx.emit(
                agent_id=self.agent_id,
                turn_id=None,
                channel="agent",
                kind="agent_status",
                data={
                    "state": "starting",
                    "step": "container",
                    "message": "Starting the agent's container…",
                },
            )
        await runtime.ensure_running(agent)
        if before != "running":
            self.ctx.emit(
                agent_id=self.agent_id,
                turn_id=None,
                channel="agent",
                kind="agent_status",
                data={"state": "running", "step": "container", "message": "The container is running."},
            )

    async def _open_thread(self, codex: AsyncCodex, agent: dict, cwd: str) -> "AsyncThread":
        options = dict(
            model=agent["model"],
            cwd=cwd,
            sandbox=Sandbox.full_access,
            approval_mode=ApprovalMode.deny_all,
            developer_instructions=self.developer_instructions,
        )
        if self.resume_thread_id:
            try:
                return await codex.thread_resume(self.resume_thread_id, **options)
            except TransportClosedError:
                raise  # the process died; starting a new thread would fail too
            except CodexError as exc:
                if self.stop_requested or not _thread_is_gone(exc):
                    # Anything else (an active writer, a protocol error) must
                    # fail this turn: starting a new thread would overwrite
                    # chat_thread_id and lose the real conversation for good.
                    raise
                logger.warning(
                    "turn %s: could not resume thread %s (%r); starting a new one",
                    self.turn_id,
                    self.resume_thread_id,
                    exc,
                )
                self._emit("notice", {"level": "warning", "message": RESUME_FAILED_NOTICE})
            self._check_stop()
        thread = await codex.thread_start(ephemeral=self.ephemeral, **options)
        await self._thread_started(thread.id)
        return thread

    async def _thread_started(self, thread_id: str) -> None:
        if self.on_thread_started is None:
            return
        try:
            result = self.on_thread_started(thread_id)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Losing the saved thread id only means the next message starts a
            # new conversation; it must not fail the turn that is running.
            logger.exception("turn %s: on_thread_started failed", self.turn_id)

    def _handle_event(self, method: str, payload: Any) -> tuple[str, Any] | None:
        """Translate one SDK notification; returns (status, error) at turn end."""
        if method in ("item/started", "item/completed"):
            data = translate_item(payload.item)
            if data is None:
                return None
            item_id = data["item_id"]
            if method == "item/started":
                if data["item_type"] == "agent_message":
                    self.partial[item_id] = data.get("text") or ""
                self._emit("item_started", data)
            else:
                if data["item_type"] == "agent_message":
                    self.final_response = data["text"]
                # The persisted item now carries the full text, so a browser
                # that connects later gets it by replay, not by snapshot.
                self.partial.pop(item_id, None)
                self._emit("item_completed", data)
        elif method == "item/agentMessage/delta":
            self.partial[payload.item_id] = self.partial.get(payload.item_id, "") + payload.delta
            self._emit("message_delta", {"item_id": payload.item_id, "delta": payload.delta})
        elif method == "item/commandExecution/outputDelta":
            self._emit("command_output", {"item_id": payload.item_id, "delta": payload.delta})
        elif method == "error":
            if payload.will_retry:
                # Codex retries up to five times; one notice per turn is enough
                # to explain the wait, and the final failure gets its own card.
                if self._retry_notice_sent:
                    return None
                self._retry_notice_sent = True
                self._emit(
                    "notice",
                    {
                        "level": "warning",
                        "message": f"Reconnecting to the model… ({payload.error.message})",
                    },
                )
            else:
                self._turn_error = payload.error
        elif method == "turn/completed":
            turn = payload.turn
            return str(_value(turn.status)), turn.error
        return None

    def _stopped_result(self) -> tuple[str, str | None, str | None]:
        if self.stop_reason == "timeout":
            return ("failed", "timeout", self.stop_message or MESSAGES["timeout"])
        if self.stop_reason == "shutdown":
            return ("interrupted", "platform_restart", MESSAGES["platform_restart"])
        return ("interrupted", "interrupted", self.stop_message or MESSAGES["interrupted"])

    # -- bookkeeping -------------------------------------------------------

    async def _close_client(self) -> None:
        # Take the client first so stop() and run() never close it twice.
        codex, self._codex = self._codex, None
        if codex is None:
            return
        try:
            await codex.close()
        except Exception:
            logger.exception("turn %s: closing the Codex client failed", self.turn_id)
        if self._scratch_subdir is not None:
            # The transcript is already in the events table; the scratch
            # Codex home only held this run's copy of the thread.
            remove_scratch_home(agent_paths(self.ctx.settings, self.agent_id), self.turn_id)
            self._scratch_subdir = None

    async def _finish(self, result: tuple[str, str | None, str | None]) -> None:
        status, error_kind, error_message = result
        await self._close_client()
        # A reply cut off by Stop, a timeout or a crash never gets its
        # item/completed, and deltas are not persisted, so save the text that
        # streamed so far; otherwise a reloaded page shows an empty bubble.
        for item_id, text in list(self.partial.items()):
            if text:
                self._emit(
                    "item_completed",
                    {"item_id": item_id, "item_type": "agent_message", "text": text, "partial": True},
                )
        self.partial.clear()
        self.outcome = TurnOutcome(
            status=status,
            final_response=self.final_response,
            error_kind=error_kind,
            error_message=error_message,
            thread_id=self.thread_id,
        )
        now = iso(utcnow())
        try:
            with self.ctx.db.transaction() as db:
                db.execute(
                    "UPDATE turns SET status = ?, final_response = ?, error_kind = ?, "
                    "error_message = ?, finished_at = ?, "
                    "codex_thread_id = COALESCE(codex_thread_id, ?) WHERE id = ?",
                    (status, self.final_response, error_kind, error_message, now,
                     self.thread_id, self.turn_id),
                )
                db.execute("UPDATE agents SET last_active_at = ? WHERE id = ?", (now, self.agent_id))
        except sqlite3.Error:
            # Most likely the agent was deleted while the turn was stopping.
            logger.exception("turn %s: could not record the outcome", self.turn_id)
        # No awaits from here on: whoever awaits run() (start_chat_turn's task,
        # the scheduler) unregisters the runner in the same loop step as these
        # events are published, so a browser reacting to turn_completed never
        # sees the agent as still busy.
        self._emit(
            "turn_completed",
            {
                "status": status,
                "final_response": self.final_response,
                "error_kind": error_kind,
                "error_message": error_message,
            },
        )
        if status == "failed" and error_message:
            self._emit("notice", {"level": "error", "message": error_message})
        self._finished.set()

    def _save_codex_ids(self) -> None:
        try:
            self.ctx.db.execute(
                "UPDATE turns SET codex_thread_id = ?, codex_turn_id = ? WHERE id = ?",
                (self.thread_id, self.codex_turn_id, self.turn_id),
            )
        except sqlite3.Error:
            logger.exception("turn %s: could not save Codex ids", self.turn_id)

    def _emit(self, kind: str, data: dict) -> None:
        try:
            self.ctx.emit(
                agent_id=self.agent_id,
                turn_id=self.turn_id,
                channel=self.channel,
                kind=kind,
                data=data,
            )
        except sqlite3.Error:
            # A persisted event for a deleted agent fails its foreign key;
            # there is nobody left to show it to.
            logger.exception("turn %s: could not emit %s", self.turn_id, kind)

    def _emit_container_failed(self, exc: AgentRuntimeError) -> None:
        # Undo the "starting" status the browser may be showing.
        try:
            self.ctx.emit(
                agent_id=self.agent_id,
                turn_id=None,
                channel="agent",
                kind="agent_status",
                data={
                    "state": "stopped",
                    "step": "container",
                    "message": f"The agent's container could not start: {exc.message}",
                },
            )
        except sqlite3.Error:
            logger.exception("turn %s: could not emit agent_status", self.turn_id)


# --------------------------------------------------------------------------
# Registry of running turns
# --------------------------------------------------------------------------


class TurnRegistry:
    """Which turns are running right now, by agent (chat) and by job run."""

    def __init__(self) -> None:
        self.chat: dict[str, TurnRunner] = {}  # agent id -> chat runner
        self.runs: dict[str, TurnRunner] = {}  # job run id -> run runner
        # Onboarding probes, kept apart so they are stopped on delete and
        # shutdown but never shown as chat or job activity.
        self.probes: dict[str, TurnRunner] = {}  # turn id -> probe runner
        # False once shutdown begins, so finishing turns do not start queued ones.
        self.accepting = True

    def chat_busy(self, agent_id: str) -> bool:
        return agent_id in self.chat

    def active_for_agent(self, agent_id: str) -> list[TurnRunner]:
        """The agent's chat runner (if any) followed by its job-run runners."""
        active = [r for r in self.runs.values() if r.agent_id == agent_id]
        chat = self.chat.get(agent_id)
        return ([chat] if chat is not None else []) + active

    async def stop_agent(self, agent_id: str, reason: str = "user") -> None:
        runners = self.active_for_agent(agent_id)
        runners += [r for r in self.probes.values() if r.agent_id == agent_id]
        await self._stop_many(runners, reason)

    async def stop_all(self, reason: str = "shutdown") -> None:
        self.accepting = False
        runners = list(self.chat.values()) + list(self.runs.values()) + list(self.probes.values())
        await self._stop_many(runners, reason)

    @staticmethod
    async def _stop_many(runners: list[TurnRunner], reason: str) -> None:
        if not runners:
            return
        results = await asyncio.gather(*(r.stop(reason) for r in runners), return_exceptions=True)
        for runner, res in zip(runners, results):
            if isinstance(res, BaseException):
                logger.error("stopping turn %s failed", runner.turn_id, exc_info=res)


# --------------------------------------------------------------------------
# Entry points used by the routers and onboarding
# --------------------------------------------------------------------------


def _insert_turn(ctx: "AppContext", agent: dict, *, turn_id: str, source: str, prompt: str) -> None:
    ctx.db.execute(
        "INSERT INTO turns (id, agent_id, source, job_run_id, conversation_no, prompt, status, "
        "started_at) VALUES (?, ?, ?, NULL, ?, ?, 'running', ?)",
        (turn_id, agent["id"], source, int(agent["conversation_no"]), prompt, iso(utcnow())),
    )


MAX_QUEUED_MESSAGES = 10
INTERRUPTED_BY_NEWER = "Interrupted by a newer message."


class QueueFull(Exception):
    """The agent already has MAX_QUEUED_MESSAGES waiting."""


def queued_turns(ctx: "AppContext", agent_id: str) -> list[dict]:
    """The agent's waiting chat messages, next one first.

    Interrupting messages (priority 1) run before ordinary queued ones
    (priority 0); within a priority, oldest first. While queued, a turn's
    started_at holds the time it was queued.
    """
    return ctx.db.all(
        "SELECT id, prompt, queue_priority, started_at FROM turns "
        "WHERE agent_id = ? AND source = 'chat' AND status = 'queued' "
        "ORDER BY queue_priority DESC, started_at ASC",
        (agent_id,),
    )


def queue_out(row: dict) -> dict:
    return {
        "turn_id": row["id"],
        "prompt": row["prompt"],
        "priority": int(row["queue_priority"]),
        "queued_at": row["started_at"],
    }


async def submit_chat_message(
    ctx: "AppContext", agent: dict, text: str, *, mode: str = "queue"
) -> dict:
    """Run a chat message now if the agent is free, otherwise queue it.

    mode "queue" waits behind earlier messages. mode "interrupt" goes to the
    front of the queue and stops the turn that is running, so it starts as
    soon as that turn has ended. Returns {turn_id, queued, position}.
    Raises QueueFull. No await happens between the busy check and starting
    or queueing, so concurrent submissions keep their order.
    """
    if mode not in ("queue", "interrupt"):
        raise ValueError(f"unknown mode {mode!r}")
    agent_id = agent["id"]
    waiting = queued_turns(ctx, agent_id)
    if not ctx.turns.chat_busy(agent_id) and not waiting:
        turn_id = new_id()
        _insert_chat_turn(ctx, agent, turn_id=turn_id, prompt=text, status="running", priority=0)
        _launch_chat_turn(ctx, agent, turn_id, text)
        return {"turn_id": turn_id, "queued": False, "position": 0}

    if len(waiting) >= MAX_QUEUED_MESSAGES:
        raise QueueFull(
            f"The queue is full ({MAX_QUEUED_MESSAGES} messages). "
            "Wait for some to finish, or cancel queued messages."
        )
    priority = 1 if mode == "interrupt" else 0
    turn_id = new_id()
    _insert_chat_turn(ctx, agent, turn_id=turn_id, prompt=text, status="queued", priority=priority)
    ctx.emit(
        agent_id=agent_id, turn_id=turn_id, channel="chat", kind="turn_queued",
        data={"prompt": text, "priority": priority, "mode": mode},
    )
    position = [r["id"] for r in queued_turns(ctx, agent_id)].index(turn_id)

    runner = ctx.turns.chat.get(agent_id)
    if runner is not None and mode == "interrupt":
        # Stopping can take a few seconds; do it in the background. When the
        # runner finishes, its task starts the queue, which begins with this.
        ctx.spawn(runner.stop("user", message=INTERRUPTED_BY_NEWER), name=f"interrupt-{turn_id}")
    elif runner is None:
        # Messages were waiting but nothing was running (for example right
        # after a restart): start the queue now.
        dispatch_next(ctx, agent_id)
    return {"turn_id": turn_id, "queued": True, "position": position}


def cancel_queued(ctx: "AppContext", agent_id: str, turn_id: str, reason: str = "Cancelled.") -> bool:
    """Remove one waiting message. False if it is not queued (any more)."""
    cur = ctx.db.execute(
        "UPDATE turns SET status = 'cancelled', error_kind = 'cancelled', error_message = ?, "
        "finished_at = ? WHERE id = ? AND agent_id = ? AND source = 'chat' AND status = 'queued'",
        (reason, iso(utcnow()), turn_id, agent_id),
    )
    if not cur.rowcount:
        return False
    ctx.emit(agent_id=agent_id, turn_id=turn_id, channel="chat", kind="turn_cancelled",
             data={"reason": reason})
    return True


def cancel_queue(ctx: "AppContext", agent_id: str, reason: str = "Cancelled.") -> int:
    """Remove every waiting message for the agent; returns how many."""
    return sum(cancel_queued(ctx, agent_id, row["id"], reason) for row in queued_turns(ctx, agent_id))


def dispatch_next(ctx: "AppContext", agent_id: str) -> str | None:
    """Start the agent's next queued message if nothing is running.

    Called when a chat turn ends and at startup. Synchronous on purpose: with
    no await inside, two callers can never both start a turn.
    """
    if not ctx.turns.accepting or ctx.turns.chat_busy(agent_id):
        return None
    agent = ctx.get_agent(agent_id)
    if agent is None or agent["setup_state"] != "ready":
        return None
    for row in queued_turns(ctx, agent_id):
        # The guarded UPDATE loses to a cancel that got there first.
        cur = ctx.db.execute(
            "UPDATE turns SET status = 'running', started_at = ?, conversation_no = ? "
            "WHERE id = ? AND status = 'queued'",
            (iso(utcnow()), int(agent["conversation_no"]), row["id"]),
        )
        if cur.rowcount:
            _launch_chat_turn(ctx, agent, row["id"], row["prompt"])
            return row["id"]
    return None


def resume_queues(ctx: "AppContext") -> int:
    """At startup, start the queue of every agent that has waiting messages."""
    rows = ctx.db.all(
        "SELECT DISTINCT agent_id FROM turns WHERE source = 'chat' AND status = 'queued'"
    )
    return sum(1 for r in rows if dispatch_next(ctx, r["agent_id"]))


def _insert_chat_turn(
    ctx: "AppContext", agent: dict, *, turn_id: str, prompt: str, status: str, priority: int
) -> None:
    ctx.db.execute(
        "INSERT INTO turns (id, agent_id, source, job_run_id, conversation_no, prompt, status, "
        "queue_priority, started_at) VALUES (?, ?, 'chat', NULL, ?, ?, ?, ?, ?)",
        (turn_id, agent["id"], int(agent["conversation_no"]), prompt, status, priority,
         iso(utcnow())),
    )


def _launch_chat_turn(ctx: "AppContext", agent: dict, turn_id: str, text: str) -> None:
    """Start a chat turn whose row is already 'running'. No awaits inside."""
    agent_id = agent["id"]
    conversation_no = int(agent["conversation_no"])
    ctx.emit(
        agent_id=agent_id,
        turn_id=turn_id,
        channel="chat",
        kind="turn_started",
        data={"source": "chat", "prompt": text},
    )

    def save_thread(thread_id: str) -> None:
        # Guarded by conversation_no so a turn from an old conversation can
        # never overwrite the thread of a newer one.
        ctx.db.execute(
            "UPDATE agents SET chat_thread_id = ? WHERE id = ? AND conversation_no = ?",
            (thread_id, agent_id, conversation_no),
        )

    runner = TurnRunner(
        ctx,
        agent=agent,
        turn_id=turn_id,
        prompt=text,
        source="chat",
        channel="chat",
        thread_id=agent.get("chat_thread_id"),
        developer_instructions=developer_instructions(agent["name"], purpose="chat"),
        on_thread_started=save_thread,
    )
    ctx.turns.chat[agent_id] = runner  # registered before the task exists

    async def run_then_next() -> None:
        try:
            await runner.run()
        finally:
            if ctx.turns.chat.get(agent_id) is runner:
                del ctx.turns.chat[agent_id]
            try:
                dispatch_next(ctx, agent_id)  # the queue continues
            except Exception:
                logger.exception("starting the next queued message for agent %s failed", agent_id)

    ctx.spawn(run_then_next(), name=f"chat-turn-{turn_id}")


async def start_chat_turn(ctx: "AppContext", agent: dict, text: str) -> str:
    """Start a chat turn now, or raise Busy (kept for callers that never queue)."""
    if ctx.turns.chat_busy(agent["id"]) or queued_turns(ctx, agent["id"]):
        raise Busy("A chat turn is already running for this agent.")
    return (await submit_chat_message(ctx, agent, text))["turn_id"]


async def run_probe(ctx: "AppContext", agent: dict) -> TurnOutcome:
    """One tiny real turn on a throwaway thread to prove the agent works."""
    timeout = ctx.settings.probe_timeout_seconds
    turn_id = new_id()
    _insert_turn(ctx, agent, turn_id=turn_id, source="probe", prompt=PROBE_PROMPT)
    ctx.emit(
        agent_id=agent["id"],
        turn_id=turn_id,
        channel="job",
        kind="turn_started",
        data={"source": "probe", "prompt": PROBE_PROMPT},
    )
    runner = TurnRunner(
        ctx,
        agent=agent,
        turn_id=turn_id,
        prompt=PROBE_PROMPT,
        source="probe",
        channel="job",
        thread_id=None,
        developer_instructions=developer_instructions(agent["name"], purpose="probe"),
        ephemeral=True,
        scratch_home=True,
    )
    ctx.turns.probes[turn_id] = runner
    task = asyncio.ensure_future(runner.run())
    try:
        # asyncio.wait (not wait_for) so a timeout goes through stop(), which
        # records the turn as failed/timeout rather than merely cancelled.
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            await runner.stop(
                "timeout", message=f"The model did not answer within {timeout} seconds."
            )
            done, _ = await asyncio.wait({task}, timeout=STOP_WAIT_SECONDS)
            if not done:
                task.cancel()
                return TurnOutcome(
                    "failed", None, "timeout",
                    f"The model did not answer within {timeout} seconds.", runner.thread_id,
                )
        return task.result()
    except asyncio.CancelledError:
        # Setup was cancelled (agent deleted, shutdown): end the probe too.
        await runner.stop("user")
        raise
    finally:
        ctx.turns.probes.pop(turn_id, None)

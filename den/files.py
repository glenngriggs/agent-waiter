"""Workspace file routes: browse, preview, download, upload, mkdir, delete.

The workspace is a bind mount the agent can write to, so anything in it may
be hostile: symlinks to /etc, FIFOs, names with odd bytes. Every path from
the browser is therefore walked one component at a time with `O_NOFOLLOW`
relative to an open directory fd. No route ever resolves a path string
against the host filesystem, so `..`, absolute paths and symlinks (even ones
swapped in mid-request) can never reach outside the workspace.
"""

from __future__ import annotations

import asyncio
import codecs
import errno
import logging
import os
import secrets
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from .agent_files import agent_paths
from .auth import owned_agent
from .context import AppContext, get_ctx
from .db import iso

logger = logging.getLogger("den.files")

router = APIRouter(prefix="/api/agents/{agent_id}/files")

MAX_PATH_CHARS = 4096
MAX_NAME_BYTES = 255
CHUNK = 64 * 1024
PREVIEW_SNIFF_BYTES = 8 * 1024
PREVIEW_TEXT_BYTES = 1024 * 1024
# Upload data is buffered in memory up to this size before a write in a
# worker thread, so a 100 MB upload costs ~100 thread hops, not ~1600.
UPLOAD_FLUSH_BYTES = 1024 * 1024

# The only types ever served inline. SVG is deliberately absent: it can carry
# script, and these four cannot.
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK so opening a FIFO the agent planted returns at once (and is then
# rejected by the S_ISREG check) instead of hanging a worker thread forever.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_TMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

# Shown whenever a walk hits a symlink: links are listed but never followed.
_SYMLINK_DETAIL = "Not found (symbolic links in the workspace are not followed)."


# --------------------------------------------------------------------------
# Path handling
# --------------------------------------------------------------------------


def _bad_path(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def check_component(name: str) -> str:
    """Validate one path component (also used for uploaded file names)."""
    if name in ("", ".", ".."):
        raise _bad_path("Path components cannot be empty, '.' or '..'.")
    if "\x00" in name or "/" in name or "\\" in name:
        raise _bad_path("File names cannot contain NUL bytes, '/' or '\\'.")
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:
        raise _bad_path("File names must be valid UTF-8.") from None
    if len(encoded) > MAX_NAME_BYTES:
        raise _bad_path(f"File names can be at most {MAX_NAME_BYTES} bytes long.")
    return name


def split_rel_path(raw: str | None) -> list[str]:
    """Split a workspace-relative path into validated components.

    Empty (or a bare "/") is the workspace root. Trailing slashes are
    tolerated so "docs/" works. A leading slash on anything else is an
    absolute path and is rejected rather than silently re-rooted, so the
    caller gets a clear 400 instead of a confusing 404.
    """
    raw = raw or ""
    if len(raw) > MAX_PATH_CHARS:
        raise _bad_path(f"Paths can be at most {MAX_PATH_CHARS} characters long.")
    if "\x00" in raw or "\\" in raw:
        raise _bad_path("Paths cannot contain NUL bytes or backslashes.")
    if raw.strip("/") == "":
        return []
    if raw.startswith("/"):
        raise _bad_path("Paths must be relative to the workspace.")
    return [check_component(part) for part in raw.rstrip("/").split("/")]


def _join(parts: list[str]) -> str:
    return "/".join(parts)


def _walk_error(exc: OSError, parts: list[str]) -> HTTPException:
    """Translate an OSError from opening a path component into an HTTP error."""
    shown = _join(parts) or "/"
    if exc.errno == errno.ELOOP:
        return HTTPException(status_code=404, detail=_SYMLINK_DETAIL)
    if exc.errno == errno.ENOENT:
        return HTTPException(status_code=404, detail=f"'{shown}' was not found.")
    if exc.errno == errno.ENOTDIR:
        return HTTPException(status_code=409, detail=f"'{shown}' is not a folder.")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return HTTPException(status_code=403, detail=f"Permission denied for '{shown}'.")
    logger.error("unexpected error opening workspace path %r: %s", shown, exc)
    return HTTPException(status_code=500, detail=f"Could not open '{shown}': {exc.strerror}.")


def _is_symlink_at(dir_fd: int, name: str) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False  # vanished meanwhile; the caller reports the original error


def open_dir_fd(root: Path, parts: list[str]) -> int:
    """Open `root/parts...` as a directory without following any symlink.

    Each component is opened relative to its parent's fd with O_NOFOLLOW, so
    a symlink anywhere in the path fails with ELOOP (mapped to 404) even if
    the agent swaps one in between our checks. The caller owns the result.
    """
    try:
        fd = os.open(root, _DIR_FLAGS)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail="The agent's workspace does not exist yet."
        ) from None
    for i, name in enumerate(parts):
        try:
            child = os.open(name, _DIR_FLAGS, dir_fd=fd)
        except OSError as exc:
            if exc.errno == errno.ENOTDIR and _is_symlink_at(fd, name):
                # Linux reports ENOTDIR, not ELOOP, for O_DIRECTORY|O_NOFOLLOW
                # on a symlink. The open was refused either way; this lstat
                # only picks the right message.
                exc = OSError(errno.ELOOP, os.strerror(errno.ELOOP))
            os.close(fd)
            raise _walk_error(exc, parts[: i + 1]) from None
        os.close(fd)
        fd = child
    return fd


def _open_parent(root: Path, parts: list[str]) -> tuple[int, str]:
    """Open the parent directory of a non-root path; returns (fd, last name)."""
    if not parts:
        raise AssertionError("caller must handle the workspace root itself")
    return open_dir_fd(root, parts[:-1]), parts[-1]


def _workspace(ctx: AppContext, agent: dict) -> Path:
    return agent_paths(ctx.settings, agent["id"]).workspace


def _mtime_iso(st: os.stat_result) -> str:
    return iso(datetime.fromtimestamp(st.st_mtime, timezone.utc))


# --------------------------------------------------------------------------
# Reading files
# --------------------------------------------------------------------------


@dataclass
class _OpenFile:
    """A regular file opened safely, plus its fstat result."""

    fd: int
    size: int
    closed: bool = field(default=False)

    def close(self) -> None:
        # Idempotent: both the stream generator and the response wrapper call
        # this, whichever finishes first.
        if not self.closed:
            self.closed = True
            os.close(self.fd)


def _open_regular_file(root: Path, parts: list[str]) -> _OpenFile:
    """Open a workspace file for reading; 409 for folders, 404 for links/missing."""
    if not parts:
        raise HTTPException(status_code=409, detail="The workspace root is a folder.")
    parent, name = _open_parent(root, parts)
    try:
        try:
            fd = os.open(name, _READ_FLAGS, dir_fd=parent)
        except OSError as exc:
            raise _walk_error(exc, parts) from None
    finally:
        os.close(parent)
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if stat.S_ISDIR(st.st_mode):
        os.close(fd)
        raise HTTPException(status_code=409, detail=f"'{_join(parts)}' is a folder.")
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise HTTPException(
            status_code=409, detail=f"'{_join(parts)}' is not a regular file."
        )
    return _OpenFile(fd=fd, size=st.st_size)


def _read_up_to(fd: int, limit: int) -> bytes:
    """Read until `limit` bytes or end of file (os.read may return short)."""
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        data = os.read(fd, min(remaining, CHUNK))
        if not data:
            break
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _image_type(name: str) -> str | None:
    return IMAGE_TYPES.get(os.path.splitext(name)[1].lower())


def _preview_sync(root: Path, parts: list[str]) -> dict[str, Any]:
    handle = _open_regular_file(root, parts)
    try:
        out: dict[str, Any] = {"path": _join(parts), "size": handle.size}
        if _image_type(parts[-1]):
            out["kind"] = "image"
            return out
        head = _read_up_to(handle.fd, PREVIEW_SNIFF_BYTES)
        if b"\x00" in head:
            out["kind"] = "binary"
            return out
        # Read one byte past the limit to learn whether the text is truncated
        # without trusting st_size (the agent may be appending right now).
        rest = _read_up_to(handle.fd, PREVIEW_TEXT_BYTES + 1 - len(head))
        data = head + rest
        truncated = len(data) > PREVIEW_TEXT_BYTES
        data = data[:PREVIEW_TEXT_BYTES]
        # An incremental decoder with final=False drops a multi-byte character
        # cut in half at the 1 MiB boundary instead of showing U+FFFD for it.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        out["kind"] = "text"
        out["text"] = decoder.decode(data, final=not truncated)
        out["truncated"] = truncated
        return out
    finally:
        handle.close()


def _content_disposition(disposition: str, name: str) -> str:
    """RFC 6266 header with an ASCII fallback and an exact UTF-8 filename*."""
    fallback = "".join(
        ch if 0x20 <= ord(ch) < 0x7F and ch not in '"\\' else "_" for ch in name
    )
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


class _FileStreamResponse(StreamingResponse):
    """StreamingResponse that always closes the file descriptor.

    Starlette cancels the streaming task when the client disconnects, which
    can leave the body generator suspended at a `yield` and never finalized;
    closing in `__call__`'s finally covers that case too.
    """

    def __init__(self, handle: _OpenFile, **kwargs: Any) -> None:
        self._handle = handle
        super().__init__(self._chunks(), **kwargs)

    async def _chunks(self) -> AsyncIterator[bytes]:
        # Send at most the size fstat reported, so a file that grows while we
        # stream never overruns the Content-Length we already promised.
        remaining = self._handle.size
        try:
            while remaining > 0:
                data = await asyncio.to_thread(os.read, self._handle.fd, min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data
        finally:
            self._handle.close()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._handle.close()


# --------------------------------------------------------------------------
# Listing, mkdir, delete
# --------------------------------------------------------------------------


def _entry_type(entry: os.DirEntry) -> str:
    if entry.is_symlink():
        return "symlink"
    if entry.is_dir(follow_symlinks=False):
        return "dir"
    if entry.is_file(follow_symlinks=False):
        return "file"
    return "other"


def _list_sync(root: Path, parts: list[str]) -> dict[str, Any]:
    fd = open_dir_fd(root, parts)
    entries: list[dict[str, Any]] = []
    try:
        # scandir(fd) dups the descriptor and stats entries with fstatat
        # relative to it, never following symlinks when asked not to.
        with os.scandir(fd) as it:
            for entry in it:
                try:
                    entry.name.encode("utf-8")
                except UnicodeEncodeError:
                    # Such a name cannot be addressed through a UTF-8 query
                    # string or serialized as JSON, so it is left out.
                    logger.warning("skipping non-UTF-8 file name in workspace listing")
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # deleted between readdir and stat; nothing to list
                kind = _entry_type(entry)
                entries.append(
                    {
                        "name": entry.name,
                        "path": _join(parts + [entry.name]),
                        "type": kind,
                        "size": st.st_size if kind == "file" else None,
                        "modified": _mtime_iso(st),
                    }
                )
    finally:
        os.close(fd)
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].casefold(), e["name"]))
    return {"path": _join(parts), "entries": entries}


def _mkdir_sync(root: Path, parts: list[str]) -> None:
    if not parts:
        raise HTTPException(status_code=409, detail="The workspace root already exists.")
    parent, name = _open_parent(root, parts)
    try:
        os.mkdir(name, 0o755, dir_fd=parent)
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"'{_join(parts)}' already exists.") from None
    finally:
        os.close(parent)


def _delete_sync(root: Path, parts: list[str]) -> None:
    if not parts:
        raise _bad_path("The workspace root cannot be deleted.")
    parent, name = _open_parent(root, parts)
    shown = _join(parts)
    try:
        try:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"'{shown}' was not found.") from None
        try:
            if stat.S_ISDIR(st.st_mode):
                # rmtree with dir_fd uses the fd-based walk, which re-checks
                # each directory with fstat and refuses symlinks, so a link
                # swapped in mid-delete cannot redirect it outside.
                shutil.rmtree(name, dir_fd=parent)
            else:
                # Files, symlinks (the link itself, never its target), FIFOs.
                os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"'{shown}' was not found.") from None
        except PermissionError:
            raise HTTPException(status_code=403, detail=f"Permission denied for '{shown}'.") from None
        except OSError as exc:
            logger.error("deleting workspace path %r failed: %s", shown, exc)
            raise HTTPException(
                status_code=500, detail=f"Could not delete '{shown}': {exc.strerror or exc}."
            ) from None
    finally:
        os.close(parent)


# --------------------------------------------------------------------------
# Upload: streamed straight from the request into the workspace
# --------------------------------------------------------------------------


class _UploadSink:
    """Receives multipart parser callbacks and writes `files` parts to disk.

    The parser's callbacks are synchronous, so they only record what
    happened; `process()` then does the file IO in worker threads. Parsing
    the stream ourselves (instead of FastAPI's UploadFile) means an
    oversized file is stopped at the limit rather than first being spooled
    in full to the server's temp directory.
    """

    def __init__(self, dir_fd: int, dir_parts: list[str], limit: int) -> None:
        self.dir_fd = dir_fd
        self.dir_parts = dir_parts
        self.limit = limit
        self.saved: list[str] = []
        self._events: list[tuple[str, bytes]] = []
        self._header_field = b""
        self._header_value = b""
        self._headers: dict[bytes, bytes] = {}
        # State of the file part currently being written, if any.
        self._name: str | None = None
        self._tmp: str | None = None
        self._fd: int | None = None
        self._written = 0
        self._buffer = bytearray()

    # -- parser callbacks (synchronous, record only) --------------------
    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self._on_part_begin,
            "on_header_field": self._on_header_field,
            "on_header_value": self._on_header_value,
            "on_header_end": self._on_header_end,
            "on_headers_finished": self._on_headers_finished,
            "on_part_data": self._on_part_data,
            "on_part_end": self._on_part_end,
        }

    def _on_part_begin(self) -> None:
        self._headers = {}
        self._header_field = b""
        self._header_value = b""

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _on_header_end(self) -> None:
        self._headers[self._header_field.lower()] = self._header_value
        self._header_field = b""
        self._header_value = b""

    def _on_headers_finished(self) -> None:
        self._events.append(("headers", self._headers.get(b"content-disposition", b"")))

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        self._events.append(("data", data[start:end]))

    def _on_part_end(self) -> None:
        self._events.append(("end", b""))

    # -- async processing ----------------------------------------------
    async def process(self) -> None:
        events, self._events = self._events, []
        for kind, payload in events:
            if kind == "headers":
                await self._begin_file(payload)
            elif kind == "data":
                await self._write(payload)
            else:
                await self._finish_file()

    async def _begin_file(self, disposition: bytes) -> None:
        _, options = parse_options_header(disposition)
        if options.get(b"name") != b"files" or b"filename" not in options:
            return  # some other form field; its data is ignored
        try:
            raw_name = options[b"filename"].decode("utf-8")
        except UnicodeDecodeError:
            raise _bad_path("Uploaded file names must be valid UTF-8.") from None
        # Browsers send just the name, but some clients send a full path; keep
        # only its last component, then apply the normal component checks.
        last = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
        name = check_component(last)
        tmp = f".{name}.upload-{secrets.token_hex(8)}"
        if len(tmp.encode("utf-8")) > MAX_NAME_BYTES:
            # Long names still need a temp name that fits; the prefix only
            # needs to be unique, not readable.
            tmp = f".upload-{secrets.token_hex(16)}"
        self._fd = await asyncio.to_thread(os.open, tmp, _TMP_FLAGS, 0o644, dir_fd=self.dir_fd)
        self._tmp, self._name, self._written = tmp, name, 0
        self._buffer.clear()

    async def _write(self, data: bytes) -> None:
        if self._fd is None:
            return
        self._written += len(data)
        if self._written > self.limit:
            raise HTTPException(
                status_code=413,
                detail=f"'{self._name}' is larger than the "
                f"{self.limit // (1024 * 1024)} MB upload limit.",
            )
        self._buffer += data
        if len(self._buffer) >= UPLOAD_FLUSH_BYTES:
            await self._flush()

    async def _flush(self) -> None:
        if self._fd is None or not self._buffer:
            return
        data, self._buffer = bytes(self._buffer), bytearray()
        await asyncio.to_thread(_write_all, self._fd, data)

    async def _finish_file(self) -> None:
        if self._fd is None:
            return
        await self._flush()
        fd, tmp, name = self._fd, self._tmp, self._name
        self._fd = None
        os.close(fd)
        try:
            # rename() replaces an existing file or symlink named `name`
            # itself; it never writes through a symlink to its target.
            await asyncio.to_thread(
                os.rename, tmp, name, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd
            )
        except OSError as exc:
            await asyncio.to_thread(_unlink_quietly, self.dir_fd, tmp)
            self._tmp = None
            # EISDIR: a folder already has this name (rename won't replace it).
            if exc.errno in (errno.EISDIR, errno.EEXIST, errno.ENOTEMPTY):
                raise HTTPException(
                    status_code=409, detail=f"A folder named '{name}' already exists here."
                ) from None
            raise
        self._tmp = None
        path = _join(self.dir_parts + [name])
        if path not in self.saved:
            self.saved.append(path)

    async def abort(self) -> None:
        """Close and delete a half-written temp file after any failure."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._tmp is not None:
            await asyncio.to_thread(_unlink_quietly, self.dir_fd, self._tmp)
            self._tmp = None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _unlink_quietly(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass  # already gone, which is the state we wanted
    except OSError as exc:
        logger.error("could not remove temporary upload file %r: %s", name, exc)


async def _receive_upload(request: Request, sink: _UploadSink, boundary: bytes) -> None:
    parser = MultipartParser(boundary, sink.callbacks())
    try:
        async for chunk in request.stream():
            parser.write(chunk)
            await sink.process()
        parser.finalize()
        await sink.process()
    except FormParserError:
        raise _bad_path("The upload is not valid multipart/form-data.") from None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


class MkdirIn(BaseModel):
    path: str


@router.get("")
async def list_files(
    path: str = Query(""),
    agent: dict = Depends(owned_agent),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    parts = split_rel_path(path)
    return await asyncio.to_thread(_list_sync, _workspace(ctx, agent), parts)


@router.get("/preview")
async def preview_file(
    path: str = Query(""),
    agent: dict = Depends(owned_agent),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    parts = split_rel_path(path)
    return await asyncio.to_thread(_preview_sync, _workspace(ctx, agent), parts)


@router.get("/download")
async def download_file(
    path: str = Query(""),
    inline: bool = Query(False),
    agent: dict = Depends(owned_agent),
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    parts = split_rel_path(path)
    handle = await asyncio.to_thread(_open_regular_file, _workspace(ctx, agent), parts)
    try:
        name = parts[-1]
        image_type = _image_type(name)
        # Only the four script-free image types are ever inline; everything
        # else (HTML, SVG, PDFs...) is an opaque attachment so it cannot run
        # on our origin.
        disposition = "inline" if inline and image_type else "attachment"
        headers = {
            "Content-Disposition": _content_disposition(disposition, name),
            "Content-Length": str(handle.size),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        return _FileStreamResponse(
            handle, headers=headers, media_type=image_type or "application/octet-stream"
        )
    except BaseException:
        handle.close()  # the response never took ownership of the fd
        raise


@router.post("/upload", status_code=201)
async def upload_files(
    request: Request,
    path: str = Query(""),
    agent: dict = Depends(owned_agent),
    ctx: AppContext = Depends(get_ctx),
) -> JSONResponse:
    parts = split_rel_path(path)
    content_type, params = parse_options_header(request.headers.get("content-type"))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise _bad_path("Uploads must be sent as multipart/form-data.")
    limit = ctx.settings.max_upload_mb * 1024 * 1024
    dir_fd = await asyncio.to_thread(open_dir_fd, _workspace(ctx, agent), parts)
    sink = _UploadSink(dir_fd, parts, limit)
    try:
        await _receive_upload(request, sink, boundary)
    except ClientDisconnect:
        logger.info("upload to agent %s cancelled: client disconnected", agent["id"])
        raise
    finally:
        # Runs on success too (a no-op then); on any failure it removes the
        # half-written temp file so nothing partial is left in the workspace.
        await sink.abort()
        os.close(dir_fd)
    if not sink.saved:
        raise _bad_path("No files were uploaded (send them in the form field 'files').")
    return JSONResponse(status_code=201, content={"saved": sink.saved})


@router.post("/mkdir", status_code=201)
async def make_directory(
    body: MkdirIn,
    agent: dict = Depends(owned_agent),
    ctx: AppContext = Depends(get_ctx),
) -> JSONResponse:
    parts = split_rel_path(body.path)
    await asyncio.to_thread(_mkdir_sync, _workspace(ctx, agent), parts)
    return JSONResponse(status_code=201, content={"path": _join(parts)})


@router.delete("", status_code=204)
async def delete_path(
    path: str = Query(""),
    agent: dict = Depends(owned_agent),
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    parts = split_rel_path(path)
    await asyncio.to_thread(_delete_sync, _workspace(ctx, agent), parts)
    return Response(status_code=204)

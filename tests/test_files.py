"""The workspace file API: browse, preview, download, upload, mkdir, delete.

The symlink tests plant links the way an agent could (pointing at /etc and at
files outside the workspace) and check that no route follows them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from den.agent_files import agent_paths
from tests.conftest import create_ready_agent, make_settings, register, running_server

# A minimal valid PNG (1x1 transparent pixel).
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
OUTSIDE_SECRET = b"host secret, never served"


@pytest.fixture(scope="module")
def server(fake_model, tmp_path_factory):
    # A 1 MiB upload limit keeps the 413 test fast; the other uploads are tiny.
    settings = make_settings(
        tmp_path_factory.mktemp("files"), fake_model, onboarding_probe=False, max_upload_mb=1
    )
    with running_server(settings) as s:
        yield s


@pytest.fixture
def setup(server, client_factory, tmp_path):
    """A fresh user and ready agent per test; returns (client, base url, workspace)."""
    c = client_factory(server)
    register(c)
    agent = create_ready_agent(c, "Filer")
    workspace = agent_paths(server.settings, agent["id"]).workspace
    assert workspace.is_dir()
    return c, f"/api/agents/{agent['id']}/files", workspace


def upload(c, base: str, files: list[tuple[str, bytes]], path: str = ""):
    return c.post(
        f"{base}/upload", params={"path": path}, files=[("files", (n, data)) for n, data in files]
    )


def listing(c, base: str, path: str = "") -> list[dict]:
    r = c.get(base, params={"path": path})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == path
    return r.json()["entries"]


# --------------------------------------------------------------------------
# The happy paths
# --------------------------------------------------------------------------


def test_upload_and_list(setup) -> None:
    c, base, workspace = setup
    r = upload(c, base, [("b.txt", b"bee"), ("a.txt", b"ay ay")])
    assert r.status_code == 201, r.text
    assert sorted(r.json()["saved"]) == ["a.txt", "b.txt"]
    assert (workspace / "a.txt").read_bytes() == b"ay ay"

    assert c.post(f"{base}/mkdir", json={"path": "zdir"}).status_code == 201
    entries = listing(c, base)
    assert [e["name"] for e in entries] == ["zdir", "a.txt", "b.txt"]  # directories first
    by_name = {e["name"]: e for e in entries}
    assert by_name["zdir"]["type"] == "dir"
    a_txt = by_name["a.txt"]
    assert (a_txt["type"], a_txt["size"], a_txt["path"]) == ("file", 5, "a.txt")
    assert a_txt["modified"]


def test_upload_into_a_subdirectory_and_replace(setup) -> None:
    c, base, workspace = setup
    r = c.post(f"{base}/mkdir", json={"path": "docs"})
    assert r.status_code == 201 and r.json()["path"] == "docs"
    r = upload(c, base, [("note.txt", b"first")], path="docs")
    assert r.status_code == 201 and r.json()["saved"] == ["docs/note.txt"]
    r = upload(c, base, [("note.txt", b"second version")], path="docs")
    assert r.status_code == 201
    assert (workspace / "docs" / "note.txt").read_bytes() == b"second version"
    [entry] = listing(c, base, "docs")
    assert entry["path"] == "docs/note.txt" and entry["size"] == len(b"second version")
    # No temporary upload files are left behind.
    assert sorted(os.listdir(workspace / "docs")) == ["note.txt"]


def test_mkdir_nested_and_existing(setup) -> None:
    c, base, workspace = setup
    assert c.post(f"{base}/mkdir", json={"path": "one"}).status_code == 201
    assert c.post(f"{base}/mkdir", json={"path": "one/two"}).status_code == 201
    assert (workspace / "one" / "two").is_dir()
    assert c.post(f"{base}/mkdir", json={"path": "one"}).status_code == 409
    assert c.post(f"{base}/mkdir", json={"path": "missing/child"}).status_code == 404


def test_preview_text(setup) -> None:
    c, base, workspace = setup
    (workspace / "hello.md").write_text("# Hello\nworld\n")
    r = c.get(f"{base}/preview", params={"path": "hello.md"})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "text"
    assert body["text"] == "# Hello\nworld\n"
    assert body["size"] == len("# Hello\nworld\n")
    assert not body.get("truncated")


def test_preview_truncates_large_text_at_one_mib(setup) -> None:
    c, base, workspace = setup
    (workspace / "big.txt").write_bytes(b"a" * (1024 * 1024 + 100))
    body = c.get(f"{base}/preview", params={"path": "big.txt"}).json()
    assert body["kind"] == "text"
    assert body["truncated"] is True
    assert len(body["text"]) == 1024 * 1024
    assert body["size"] == 1024 * 1024 + 100


def test_preview_binary_and_image(setup) -> None:
    c, base, workspace = setup
    (workspace / "blob.dat").write_bytes(b"\x00\x01\x02binary")
    body = c.get(f"{base}/preview", params={"path": "blob.dat"}).json()
    assert body["kind"] == "binary"
    assert "text" not in body or body["text"] is None
    (workspace / "pic.png").write_bytes(PNG)
    assert c.get(f"{base}/preview", params={"path": "pic.png"}).json()["kind"] == "image"


def test_preview_of_a_directory_or_missing_file(setup) -> None:
    c, base, workspace = setup
    (workspace / "dir").mkdir()
    assert c.get(f"{base}/preview", params={"path": "dir"}).status_code == 409
    assert c.get(f"{base}/preview", params={"path": "nope.txt"}).status_code == 404


def _assert_safe_headers(r) -> None:
    assert r.headers["x-content-type-options"] == "nosniff"
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "sandbox" in csp


def test_download_is_an_attachment_with_safe_headers(setup) -> None:
    c, base, workspace = setup
    (workspace / "report.txt").write_bytes(b"quarterly numbers")
    r = c.get(f"{base}/download", params={"path": "report.txt"})
    assert r.status_code == 200
    assert r.content == b"quarterly numbers"
    assert r.headers["content-disposition"].lower().startswith("attachment")
    _assert_safe_headers(r)


def test_inline_png_is_served_inline(setup) -> None:
    c, base, workspace = setup
    (workspace / "pic.png").write_bytes(PNG)
    r = c.get(f"{base}/download", params={"path": "pic.png", "inline": 1})
    assert r.status_code == 200
    assert r.content == PNG
    assert not r.headers.get("content-disposition", "").lower().startswith("attachment")
    assert r.headers["content-type"].startswith("image/png")
    _assert_safe_headers(r)


@pytest.mark.parametrize("name", ["page.html", "image.svg", "script.js"])
def test_inline_is_refused_for_anything_but_the_four_image_types(setup, name) -> None:
    c, base, workspace = setup
    (workspace / name).write_text("<script>alert(document.cookie)</script>")
    r = c.get(f"{base}/download", params={"path": name, "inline": 1})
    assert r.status_code == 200
    assert r.headers["content-disposition"].lower().startswith("attachment")
    _assert_safe_headers(r)


def test_download_of_a_directory_or_missing_file(setup) -> None:
    c, base, workspace = setup
    (workspace / "dir").mkdir()
    assert c.get(f"{base}/download", params={"path": "dir"}).status_code == 409
    assert c.get(f"{base}/download", params={"path": "gone.txt"}).status_code == 404


def test_list_errors(setup) -> None:
    c, base, workspace = setup
    (workspace / "file.txt").write_text("x")
    assert c.get(base, params={"path": "file.txt"}).status_code == 409
    assert c.get(base, params={"path": "no-such-dir"}).status_code == 404


def test_delete_file_and_directory(setup) -> None:
    c, base, workspace = setup
    (workspace / "f.txt").write_text("x")
    (workspace / "tree" / "deeper").mkdir(parents=True)
    (workspace / "tree" / "deeper" / "leaf.txt").write_text("leaf")

    assert c.delete(base, params={"path": "f.txt"}).status_code == 204
    assert not (workspace / "f.txt").exists()
    assert c.delete(base, params={"path": "tree"}).status_code == 204
    assert not (workspace / "tree").exists()
    assert c.delete(base, params={"path": "tree"}).status_code == 404


def test_delete_of_the_root_is_refused(setup) -> None:
    c, base, workspace = setup
    (workspace / "keep.txt").write_text("x")
    assert c.delete(base, params={"path": ""}).status_code == 400
    assert c.delete(base, params={"path": "/"}).status_code == 400
    assert (workspace / "keep.txt").exists()


# --------------------------------------------------------------------------
# Paths that must be rejected
# --------------------------------------------------------------------------

BAD_PATHS = ["..", "../..", "a/../b", "./a", "a/./b", "a//b", "a\\b", "a\x00b", "x" * 256]


@pytest.mark.parametrize("bad", BAD_PATHS, ids=[repr(p)[:20] for p in BAD_PATHS])
def test_bad_paths_are_400_on_every_route(setup, bad) -> None:
    c, base, workspace = setup
    assert c.get(base, params={"path": bad}).status_code == 400
    assert c.get(f"{base}/preview", params={"path": bad}).status_code == 400
    assert c.get(f"{base}/download", params={"path": bad}).status_code == 400
    assert upload(c, base, [("x.txt", b"x")], path=bad).status_code == 400
    assert c.post(f"{base}/mkdir", json={"path": bad}).status_code == 400
    assert c.delete(base, params={"path": bad}).status_code == 400


def test_overlong_path_is_400(setup) -> None:
    c, base, workspace = setup
    assert c.get(base, params={"path": "/".join(["d" * 200] * 25)}).status_code == 400


@pytest.mark.parametrize("absolute", ["/etc/passwd", "/etc"])
def test_absolute_paths_never_reach_the_host(setup, absolute) -> None:
    c, base, workspace = setup
    # Leading slashes are stripped, so this names a (missing) workspace path.
    r = c.get(f"{base}/download", params={"path": absolute})
    assert r.status_code in (400, 404)
    assert b"root:" not in r.content
    assert c.get(base, params={"path": absolute}).status_code in (400, 404)


def test_upload_filename_cannot_escape_the_directory(setup) -> None:
    c, base, workspace = setup
    r = upload(c, base, [("../escape.txt", b"x")])
    if r.status_code == 201:
        # Only the last path component of the filename is used.
        assert r.json()["saved"] == ["escape.txt"]
        assert (workspace / "escape.txt").exists()
    else:
        assert r.status_code == 400
    assert not (workspace.parent / "escape.txt").exists()
    assert upload(c, base, [("..", b"x")]).status_code == 400


# --------------------------------------------------------------------------
# Symlinks planted inside the workspace
# --------------------------------------------------------------------------


@pytest.fixture
def planted(setup, tmp_path):
    """Links an agent could create: to /etc, to a host file, to a host directory."""
    c, base, workspace = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_bytes(OUTSIDE_SECRET)
    os.symlink("/etc", workspace / "etc_link")
    os.symlink(secret, workspace / "file_link")
    os.symlink(outside, workspace / "dir_link")
    return c, base, workspace, outside, secret


def test_symlinks_are_listed_as_symlinks(planted) -> None:
    c, base, workspace, outside, secret = planted
    types = {e["name"]: e["type"] for e in listing(c, base)}
    assert types == {"etc_link": "symlink", "file_link": "symlink", "dir_link": "symlink"}


def _refused(r) -> None:
    assert 400 <= r.status_code < 500, r.status_code
    assert OUTSIDE_SECRET not in r.content
    assert b"root:" not in r.content


@pytest.mark.parametrize("path", ["file_link", "dir_link/secret.txt", "etc_link/passwd"])
def test_download_and_preview_never_follow_symlinks(planted, path) -> None:
    c, base, *_ = planted
    _refused(c.get(f"{base}/download", params={"path": path}))
    _refused(c.get(f"{base}/download", params={"path": path, "inline": 1}))
    _refused(c.get(f"{base}/preview", params={"path": path}))


@pytest.mark.parametrize("path", ["etc_link", "dir_link"])
def test_listing_through_a_symlink_is_404(planted, path) -> None:
    c, base, *_ = planted
    r = c.get(base, params={"path": path})
    assert r.status_code == 404
    assert b"passwd" not in r.content and b"secret.txt" not in r.content


def test_upload_and_mkdir_through_a_symlink_are_refused(planted) -> None:
    c, base, workspace, outside, secret = planted
    assert upload(c, base, [("planted.txt", b"x")], path="dir_link").status_code == 404
    assert c.post(f"{base}/mkdir", json={"path": "dir_link/newdir"}).status_code == 404
    assert sorted(os.listdir(outside)) == ["secret.txt"]


def test_upload_over_a_symlinked_name_replaces_the_link_not_the_target(planted) -> None:
    c, base, workspace, outside, secret = planted
    r = upload(c, base, [("file_link", b"new content")])
    if r.status_code == 201:
        assert not (workspace / "file_link").is_symlink()
        assert (workspace / "file_link").read_bytes() == b"new content"
    else:
        assert 400 <= r.status_code < 500
    assert secret.read_bytes() == OUTSIDE_SECRET


def test_delete_through_a_symlink_is_refused(planted) -> None:
    c, base, workspace, outside, secret = planted
    assert c.delete(base, params={"path": "dir_link/secret.txt"}).status_code == 404
    assert secret.read_bytes() == OUTSIDE_SECRET


def test_deleting_a_symlink_removes_only_the_link(planted) -> None:
    c, base, workspace, outside, secret = planted
    assert c.delete(base, params={"path": "dir_link"}).status_code == 204
    assert c.delete(base, params={"path": "file_link"}).status_code == 204
    assert c.delete(base, params={"path": "etc_link"}).status_code == 204
    assert not os.path.lexists(workspace / "dir_link")
    assert secret.read_bytes() == OUTSIDE_SECRET
    assert Path("/etc/passwd").exists()


def test_recursive_delete_does_not_follow_links_inside(planted) -> None:
    c, base, workspace, outside, secret = planted
    (workspace / "box").mkdir()
    os.symlink(outside, workspace / "box" / "inner_link")
    assert c.delete(base, params={"path": "box"}).status_code == 204
    assert not (workspace / "box").exists()
    assert secret.read_bytes() == OUTSIDE_SECRET


# --------------------------------------------------------------------------
# Size limit
# --------------------------------------------------------------------------


def test_upload_over_the_limit_is_413_and_leaves_nothing(setup) -> None:
    c, base, workspace = setup
    r = upload(c, base, [("huge.bin", b"z" * (1024 * 1024 + 512 * 1024))])
    assert r.status_code == 413
    assert os.listdir(workspace) == [], "no partial or temporary file may remain"

"""Regression tests for frontend bugs in den/static/app.js.

app.js is one IIFE with no build step, so these tests pull the functions they need out of
the source by name and run them in node against a tiny DOM stub. That keeps them fast and
free of a browser, and it tests the shipped code rather than a copy.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "den" / "static" / "app.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

AGENT_ID = "0123456789abcdef0123456789abcdef"

# Just enough DOM for h(), appendInlineMd() and mdLink().
DOM_STUB = r"""
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.style = {}; this.attrs = {}; }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  replaceChildren() { this.children = []; }
  set textContent(s) { this.children = [{ tag: "#text", text: s }]; }
}
globalThis.Node = Node;
globalThis.document = {
  createElement: (t) => new Node(t),
  createTextNode: (s) => { const n = new Node("#text"); n.text = s; return n; },
};
globalThis.location = { hash: "#/agent/%s/chat" };
function dump(n) {
  if (n.tag === "#text") return n.text;
  return { tag: n.tag, href: n.attrs.href, children: n.children.map(dump) };
}
""" % AGENT_ID


def _source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _extract_block(src: str, start: int) -> str:
    """Return src[start:] up to the brace that closes the first "{" after start."""
    open_at = src.index("{", start)
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError("unbalanced braces in app.js")


def _function(src: str, name: str) -> str:
    m = re.search(r"^  (?:async )?function %s\(" % re.escape(name), src, re.M)
    assert m, f"function {name} not found in app.js"
    return _extract_block(src, m.start())


def _const(src: str, name: str) -> str:
    m = re.search(r"^  const %s = .*;$" % re.escape(name), src, re.M)
    assert m, f"const {name} not found in app.js"
    return m.group(0)


def _method(src: str, cls: str, name: str) -> str:
    class_at = src.index(f"  class {cls} ")
    m = re.compile(r"^    %s\(" % re.escape(name), re.M).search(src, class_at)
    assert m, f"{cls}.{name} not found in app.js"
    return _extract_block(src, m.start())


def _run_node(script: str, tz: str = "UTC") -> dict:
    env = dict(os.environ, TZ=tz, LC_ALL="en_US.UTF-8", LANG="en_US.UTF-8")
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _inline_md_script(body: str) -> str:
    src = _source()
    parts = [
        DOM_STUB,
        _const(src, "DOM_PROPS"),
        _function(src, "h"),
        _function(src, "appendAll"),
        _const(src, "INLINE_RE"),
        _function(src, "appendInlineMd"),
        _function(src, "mdLink"),
        body,
    ]
    return "\n".join(parts)


@pytest.mark.parametrize("target", ["50%.txt", "100%", "a%zz", "./100%_done.csv", "/workspace/sub/x%.md"])
def test_markdown_link_with_stray_percent_does_not_throw(target):
    # Bugs #20/#26: decodeURIComponent threw URIError on a malformed escape, which escaped
    # the history replay and replaced the whole agent page with "URI malformed".
    out = _run_node(_inline_md_script(
        "const p = h('p'); appendInlineMd(p, %s); console.log(JSON.stringify(dump(p)));"
        % json.dumps(f"Saved to [report]({target})")
    ))
    link = out["children"][1]
    assert link["tag"] == "a"
    assert link["href"].startswith(f"#/agent/{AGENT_ID}/files?path=")
    assert link["children"] == ["report"]


def test_markdown_link_still_decodes_valid_escapes():
    out = _run_node(_inline_md_script(
        "const p = h('p'); appendInlineMd(p, '[n](/workspace/my%20dir/notes.txt)'); console.log(JSON.stringify(dump(p)));"
    ))
    assert out["children"][0]["href"] == f"#/agent/{AGENT_ID}/files?path=my%20dir"


def test_timeline_apply_survives_an_event_that_fails_to_render():
    # Defence in depth for #20/#26: one bad event must not escape Timeline.apply, so it can
    # neither reject session.ready during history replay nor stall a job-run transcript.
    src = _source()
    script = "\n".join([
        "class Timeline {",
        "  constructor() { this.maxSeq = 0; this.rendered = []; this.empties = 0; }",
        "  accepts() { return true; }",
        "  isNearBottom() { return false; }",
        "  scrollToBottom() {}",
        "  updateEmpty() { this.empties++; }",
        "  renderItem(ev) { if (ev.data.bad) throw new URIError('URI malformed'); this.rendered.push(ev.seq); }",
        _method(src, "Timeline", "apply"),
        _method(src, "Timeline", "applyKind") if "applyKind(" in src else "",
        "}",
        "console.error = () => {};",
        "const tl = new Timeline();",
        "let threw = null;",
        "try {",
        "  for (const [seq, bad] of [[1, false], [2, true], [3, false]])",
        "    tl.apply({ seq, kind: 'item_completed', data: { bad } });",
        "} catch (err) { threw = String(err); }",
        "console.log(JSON.stringify({ threw, rendered: tl.rendered, maxSeq: tl.maxSeq, empties: tl.empties }));",
    ])
    out = _run_node(script)
    assert out["threw"] is None
    assert out["rendered"] == [1, 3]
    assert out["maxSeq"] == 3
    assert out["empties"] == 3


@pytest.mark.parametrize("tz", ["America/New_York", "America/Los_Angeles", "UTC", "Asia/Tokyo"])
def test_usage_since_is_the_same_calendar_day_in_every_timezone(tz):
    # Bug #27: /api/usage sends `since` as a UTC date ("2026-10-08"); fmtDateTime showed it
    # as "Oct 7, 2026, 8:00 PM" west of UTC.
    src = _source()
    usage = _function(src, "mountUsageTab")
    assert "fmtDateTime(res.since)" not in usage
    assert "fmtDay(res.since)" in usage
    script = "\n".join([
        _function(src, "parseTime"),
        _function(src, "fmtDay"),
        "console.log(JSON.stringify({ day: fmtDay('2026-10-08'), bad: fmtDay('nonsense') }));",
    ])
    out = _run_node(script, tz=tz)
    assert out["day"] == "Oct 8, 2026"
    assert out["bad"] == "—"


# --- Stale "busy" state: the composer said Queue with nothing running -------------------

_SESSION_STUB = "\n".join([
    "const BUSY_CHECK_MS = 8000;",
    "let now = 1000; Date.now = () => now;",
    "const timers = []; globalThis.setInterval = (fn) => { timers.push(fn); return timers.length; };",
    "globalThis.clearInterval = () => {};",
    "class AgentSession {",
    "  constructor() {",
    "    this.busy = false; this.activeTurnId = null; this.stopping = false; this.closed = false;",
    "    this.busyChangedAt = 0; this.busyTimer = null; this.endedTurns = new Set();",
    "    this.partials = new Map(); this.dangling = 0;",
    "    this.timeline = { setWorking() {}, finishDangling: () => { this.dangling++; } };",
    "  }",
    "  notify() {} scheduleRefresh() {} dequeue() {} enqueue() {}",
])


def _session_script(body: str) -> str:
    src = _source()
    methods = [_method(src, "AgentSession", name)
               for name in ("track", "setBusy", "syncBusy", "watchBusy", "hasEnded")]
    return "\n".join([_SESSION_STUB, *methods, "}", "const s = new AgentSession();", body])


def test_a_turn_that_already_ended_is_remembered():
    # A turn that fails at once can end before the POST answer that started it is handled.
    # The composer asks hasEnded() before marking the agent busy on that answer; without
    # it the page stayed "busy" for good and Send read "Queue" with nothing running.
    out = _run_node(_session_script(
        "s.track({ kind: 'turn_started', channel: 'chat', turn_id: 't1', data: {} });"
        "const busyDuring = s.busy;"
        "s.track({ kind: 'turn_completed', channel: 'chat', turn_id: 't1', data: {} });"
        "console.log(JSON.stringify({ busyDuring, busyAfter: s.busy, ended: s.hasEnded('t1'),"
        " other: s.hasEnded('t2') }));"
    ))
    assert out == {"busyDuring": True, "busyAfter": False, "ended": True, "other": False}


def test_composer_does_not_mark_an_ended_turn_busy():
    src = _source()
    send = _extract_block(src, src.index("    async function send(mode) {"))
    assert "if (!session.hasEnded(res.turn_id)) session.setBusy(true, res.turn_id);" in send
    # "Stopping…" is only shown while the interrupted turn is still the running one.
    assert "session.activeTurnId === interrupted" in send


def test_stale_busy_heals_from_the_agent_row():
    # The page missed a turn_completed (stalled stream). The periodic check sees the server
    # is idle and clears busy; the reverse (server busy, page idle) is corrected too.
    out = _run_node(_session_script(
        "s.setBusy(true, 't1'); const watching = timers.length;"
        "now = 5000; s.syncBusy({ chat_busy: false }, 4000);"
        "const healed = { busy: s.busy, dangling: s.dangling };"
        "now = 6000; s.syncBusy({ chat_busy: true, active_turn_id: 't2' }, 5500);"
        "console.log(JSON.stringify({ watching, healed, busy: s.busy, active: s.activeTurnId }));"
    ))
    assert out == {"watching": 1, "healed": {"busy": False, "dangling": 1}, "busy": True, "active": "t2"}


def test_busy_check_ignores_an_answer_older_than_the_last_change():
    # Asked at t=4000, but a turn started at t=5000 (from our own events) before the answer
    # came back saying "idle": the answer describes the past and must not clear busy.
    out = _run_node(_session_script(
        "now = 5000; s.setBusy(true, 't1'); s.syncBusy({ chat_busy: false }, 4000);"
        "console.log(JSON.stringify({ busy: s.busy }));"
    ))
    assert out == {"busy": True}


def test_send_is_disabled_while_the_message_is_empty():
    # Clicking Send/Queue with nothing typed used to do nothing at all, silently.
    src = _source()
    assert "sendBtn.disabled = sending || empty;" in src
    assert "interruptBtn.disabled = sending || empty || session.stopping;" in src

"""Persistent chat threads for the Text mode.

Threads live server-side, one JSON file per thread in the active
workspace, so a browser reload or a container restart doesn't lose a
conversation and an MCP client sees the same threads as the UI.

Persistence follows the pipeline pattern in director_pipeline.py: write to
a process/thread-unique temp file, then os.replace() onto the target. That
makes a half-written thread impossible even if the process dies mid-save.
"""

import json
import os
import threading
import time
import uuid

THREAD_FILE_PREFIX = "_chat_"
THREAD_STATE_VERSION = 1

# Guards the file writes. Threads are small and saves are infrequent
# (one per message), so a single lock is plenty.
# ponytail: one global lock, per-thread locks if concurrent chats ever contend
_file_lock = threading.Lock()


def _thread_path(out_dir: str, tid: str) -> str:
    return os.path.join(out_dir, f"{THREAD_FILE_PREFIX}{tid}.json")


def _write_json_atomic(path: str, payload: dict) -> None:
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def create_thread(out_dir: str, title: str = "", system_prompt: str = "",
                  model_id: str = "") -> dict:
    """Create and persist an empty thread. Returns the thread dict."""
    os.makedirs(out_dir, exist_ok=True)
    now = time.time()
    thread = {
        "version": THREAD_STATE_VERSION,
        "id": uuid.uuid4().hex[:8],
        "title": (title or "New chat").strip()[:200],
        "system_prompt": system_prompt or "",
        "model_id": model_id or "",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    with _file_lock:
        _write_json_atomic(_thread_path(out_dir, thread["id"]), thread)
    return thread


def load_thread(out_dir: str, tid: str) -> dict | None:
    path = _thread_path(out_dir, tid)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("id"):
        return None
    data.setdefault("messages", [])
    return data


def save_thread(out_dir: str, thread: dict) -> None:
    thread["updated_at"] = time.time()
    with _file_lock:
        _write_json_atomic(_thread_path(out_dir, thread["id"]), thread)


def list_threads(out_dir: str) -> list[dict]:
    """Thread summaries (no message bodies), newest first."""
    out = []
    try:
        names = os.listdir(out_dir)
    except OSError:
        return out
    for name in names:
        if not (name.startswith(THREAD_FILE_PREFIX) and name.endswith(".json")):
            continue
        t = load_thread(out_dir, name[len(THREAD_FILE_PREFIX):-len(".json")])
        if not t:
            continue
        msgs = t.get("messages") or []
        out.append({
            "id": t["id"],
            "title": t.get("title") or "New chat",
            "model_id": t.get("model_id") or "",
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "message_count": len(msgs),
            "preview": (msgs[-1].get("content") or "")[:160] if msgs else "",
        })
    out.sort(key=lambda t: t.get("updated_at") or 0, reverse=True)
    return out


def delete_thread(out_dir: str, tid: str) -> bool:
    path = _thread_path(out_dir, tid)
    if not os.path.isfile(path):
        return False
    with _file_lock:
        try:
            os.remove(path)
            return True
        except OSError:
            return False


def append_message(thread: dict, role: str, content: str, **extra) -> dict:
    """Append a message in place and return it."""
    msg = {"role": role, "content": content, "at": time.time()}
    msg.update(extra)
    thread.setdefault("messages", []).append(msg)
    return msg


def title_from_first_message(text: str) -> str:
    """A thread title derived from the opening message."""
    line = " ".join((text or "").split())
    return (line[:60] + "…") if len(line) > 60 else (line or "New chat")


def build_messages(thread: dict, max_messages: int = 40) -> list[dict]:
    """Conversation for the LLM: the trailing window of the thread.

    Only role/content survive — the stored timestamps and metadata are for
    the UI, not the model. The window keeps very long threads from
    silently overflowing the context; the model's own limit still applies
    on top of it.
    """
    msgs = [m for m in (thread.get("messages") or [])
            if m.get("role") in ("user", "assistant") and m.get("content")]
    return [{"role": m["role"], "content": m["content"]} for m in msgs[-max_messages:]]


if __name__ == "__main__":
    import shutil
    import tempfile

    d = tempfile.mkdtemp(prefix="chatstore-selfcheck-")
    try:
        t = create_thread(d, title="", system_prompt="be terse")
        assert t["title"] == "New chat" and t["messages"] == []
        assert load_thread(d, t["id"])["system_prompt"] == "be terse"
        assert load_thread(d, "nope") is None

        append_message(t, "user", "hi there")
        append_message(t, "assistant", "hello", stream_id="s1")
        t["title"] = title_from_first_message("hi there")
        save_thread(d, t)

        reloaded = load_thread(d, t["id"])
        assert len(reloaded["messages"]) == 2
        assert reloaded["messages"][1]["stream_id"] == "s1"
        assert reloaded["title"] == "hi there"

        # build_messages drops metadata and keeps only the dialogue
        conv = build_messages(reloaded)
        assert conv == [{"role": "user", "content": "hi there"},
                        {"role": "assistant", "content": "hello"}], conv

        # window keeps the TAIL, which is what the model needs
        for i in range(50):
            append_message(t, "user", f"m{i}")
        windowed = build_messages(t, max_messages=5)
        assert len(windowed) == 5 and windowed[-1]["content"] == "m49", windowed
        save_thread(d, t)

        summaries = list_threads(d)
        assert len(summaries) == 1 and summaries[0]["id"] == t["id"]
        assert summaries[0]["message_count"] == 52
        assert "messages" not in summaries[0], "summaries must stay light"

        assert delete_thread(d, t["id"]) is True
        assert delete_thread(d, t["id"]) is False
        assert list_threads(d) == []
        print("chat_store self-check: OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)

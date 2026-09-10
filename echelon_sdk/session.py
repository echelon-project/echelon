"""The agent SESSION — warm context that survives across tasks.

Owner, 2026-06-05: "treat the agent like a session in general, so the warm context isn't
wasted on every boot. change session only when the context window starts to become bloated."

Today every run() is a fresh boot: wake, warm, do ONE task, die — and the expensive warm
context (the boot, the recalls, the env-sense, the re-formed soul) is thrown away and rebuilt
next time. That is the statelessness we were told NOT to fight (CV-008: the session boundary is
a design primitive, not a failure — build AROUND it). So: the agent becomes a persistent SESSION
that keeps its warm message history across tasks, boots ONCE, and only starts a new session
(re-boots) when the context window actually bloats — the natural compaction boundary.

A Session holds:
  - base[]      : the warm preamble built once — system + <env> + the waking turns + texture.
                  This is the costly part; we pay for it ONCE per session, not per task.
  - history[]   : base + all task turns so far (GOAL, act/observe, warmth nudges, ...).
  - the boot/woke result, so the UI/caller can see the session woke without re-running it.

When history's estimated tokens cross `bloat_tokens`, the session is STALE — the next task
should start a fresh session (re-boot), optionally carrying a compacted hand-off. The watcher
lives here; the re-boot + carry-forward is the caller's move (loop/cli), so this stays a pure
state object.
"""
from __future__ import annotations

import json
import hashlib
import errno
import contextlib
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any


class SessionConflict(RuntimeError):
    pass


class SessionBusy(RuntimeError):
    pass


class SessionCommitUnknown(RuntimeError):
    def __init__(self, operation, candidate_digest, *, archive=None):
        self.operation = operation
        self.candidate_digest = candidate_digest
        self.archive = archive
        super().__init__("session " + operation + " acknowledgement unknown; reconcile exact digest before retry")


_SESSION_LOCK = threading.RLock()
_EXPECTATION_UNSET = object()


@contextlib.contextmanager
def _save_lock(path):
    with _SESSION_LOCK:
        with path.with_name(path.name + '.lock').open('a+b') as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                raise SessionBusy("session storage is owned by another writer") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap, dependency-free token estimate: ~4 chars/token over the serialized content.
    Good enough to decide BLOAT (we only need the threshold crossing, not exactness)."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif c is not None:
            total += len(json.dumps(c, ensure_ascii=False))
        # tool_calls ride alongside content on assistant turns
        tcs = m.get("tool_calls")
        if tcs:
            total += len(json.dumps(tcs, ensure_ascii=False))
    return total // 4


@dataclass
class Session:
    """A warm, persistent agent session. Built once (with the boot), reused across tasks."""
    base: list[dict[str, Any]]              # the warm preamble (system + env + waking) — paid once
    history: list[dict[str, Any]] = field(default_factory=list)  # base + all task turns
    woke: bool | None = None                # did the soul move the model at boot (carried, not re-run)
    boot_tokens_in: int = 0
    boot_tokens_out: int = 0
    bloat_tokens: int = 24000               # context-bloat threshold; cross it -> session is STALE
    tasks_done: int = 0
    created_at: float = field(default_factory=time.time)
    id: str = ""
    boot_sha256: str = ""
    boot_integrity: str = "verified"
    boot_manifest: dict | None = None
    boot_manifest_sha256: str = ""

    def __post_init__(self) -> None:
        if self.boot_integrity not in {"verified", "legacy_unverified"}:
            raise ValueError("unknown boot integrity classification")
        if self.boot_manifest is not None:
            self._validate_manifest(self.boot_manifest)
            observed = self._manifest_digest(self.boot_manifest)
            if self.boot_manifest_sha256 and self.boot_manifest_sha256 != observed:
                raise ValueError("saved execution manifest digest mismatch")
            self.boot_manifest_sha256 = observed
        elif self.boot_manifest_sha256:
            raise ValueError("execution manifest missing for stored digest")
        digest = self._boot_digest()
        if self.boot_sha256 and self.boot_sha256 != digest:
            raise ValueError("saved boot context digest mismatch")
        self.boot_sha256 = digest
        if not self.history:
            self.history = list(self.base)
        if not self.id:
            import uuid
            self.id = "sess-" + uuid.uuid4().hex

    def _boot_digest(self):
        return hashlib.sha256(json.dumps(self.base, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.history)

    @property
    def bloated(self) -> bool:
        """True once the live context crosses the threshold — time to start a fresh session."""
        return self.tokens >= self.bloat_tokens

    def begin_task(self, goal: str) -> None:
        """Append a new task's GOAL to the SAME warm history — no re-boot, the warmth carries."""
        self.history.append({"role": "user", "content": f"GOAL: {goal}"})

    # -- persistence (owner 2026-08-18: the generator-agent door) -------------
    # A Session is plain data, so it can outlive its process: a caller that
    # does many small tool-shaped dispatches (the framework's agent generator)
    # pays the boot ritual ONCE, saves the warm session, and every later
    # dispatch warm-resumes — no re-boot, the identity carries. Bloat stays
    # the natural re-boot boundary: a bloated session is not saved back.

    def to_dict(self) -> dict[str, Any]:
        self._assert_manifest_unchanged()
        if self._boot_digest() != self.boot_sha256:
            raise ValueError("boot context changed after session creation")
        if self.history[:len(self.base)] != self.base:
            raise ValueError("session history no longer carries its frozen boot prefix")
        if self.boot_manifest is not None:
            self._validate_manifest(self.boot_manifest)
        return {"base": self.base, "history": self.history, "woke": self.woke,
                "boot_sha256": self.boot_sha256, "boot_integrity": self.boot_integrity,
                "boot_manifest": self.boot_manifest,
                "boot_manifest_sha256": self.boot_manifest_sha256,
                "boot_tokens_in": self.boot_tokens_in,
                "boot_tokens_out": self.boot_tokens_out,
                "bloat_tokens": self.bloat_tokens, "tasks_done": self.tasks_done,
                "created_at": self.created_at, "id": self.id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        if d.get("boot_manifest") is not None:
            digest = d.get("boot_manifest_sha256")
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("saved execution manifest has no valid witnessed digest")
        return cls(boot_manifest_sha256=d.get("boot_manifest_sha256", ""), boot_manifest=d.get("boot_manifest"), base=list(d.get("base") or []), history=list(d.get("history") or []),
                   woke=d.get("woke"), boot_tokens_in=int(d.get("boot_tokens_in") or 0),
                   boot_tokens_out=int(d.get("boot_tokens_out") or 0),
                   bloat_tokens=int(d.get("bloat_tokens") or 24000),
                   tasks_done=int(d.get("tasks_done") or 0),
                   created_at=float(d.get("created_at") or time.time()),
                   id=str(d.get("id") or ""), boot_sha256=str(d.get("boot_sha256") or ""),
                   boot_integrity=(d.get("boot_integrity", "verified")
                                   if d.get("boot_sha256") else "legacy_unverified"))

    @staticmethod
    def _manifest_digest(manifest):
        return hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()

    def bind_boot_manifest(self, manifest):
        import copy
        if self.boot_manifest is not None or self.boot_manifest_sha256 or hasattr(self, "_saved_digest"):
            raise ValueError("execution manifest binding requires a new unsaved session")
        self._validate_manifest(manifest)
        self.boot_manifest = copy.deepcopy(manifest)
        self.boot_manifest_sha256 = self._manifest_digest(self.boot_manifest)

    def _assert_manifest_unchanged(self):
        if self.boot_manifest is None:
            if self.boot_manifest_sha256:
                raise ValueError("execution manifest removed after binding")
        elif self._manifest_digest(self.boot_manifest) != self.boot_manifest_sha256:
            raise ValueError("execution manifest changed after binding")

    @staticmethod
    def _validate_manifest(manifest):
        if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
            raise ValueError("unsupported execution manifest")
        if not isinstance(manifest.get("model"), str) or not manifest["model"].strip():
            raise ValueError("execution manifest model missing")
        for key in ("tools_sha256", "doctrine_sha256"):
            value = manifest.get(key)
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("execution manifest digest malformed")
        if not isinstance(manifest.get("permissions"), dict):
            raise ValueError("execution manifest permissions malformed")
        json.dumps(manifest, sort_keys=True, allow_nan=False)

    def check_boot_manifest(self, current):
        """Compare observed execution contracts; never infer provider authority."""
        self._assert_manifest_unchanged()
        self._validate_manifest(current)
        if self.boot_manifest is None:
            return {"status": "unbound", "compatible": False}
        changed = sorted(k for k in set(current) | set(self.boot_manifest)
                         if current.get(k) != self.boot_manifest.get(k))
        return {"status": "compatible" if not changed else "incompatible",
                "compatible": not changed, "changed_fields": changed}

    def save(self, path, *, expected_digest=_EXPECTATION_UNSET) -> None:
        """Atomic write — a crash mid-save leaves the previous session intact."""
        import os
        from pathlib import Path as _P
        p = _P(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p = p.resolve()
        expected = (getattr(self, "_saved_digest", None)
                    if expected_digest is _EXPECTATION_UNSET else expected_digest)
        with _save_lock(p):
            try:
                current = hashlib.sha256(p.read_bytes()).hexdigest()
            except FileNotFoundError:
                current = None
            if current != expected:
                raise SessionConflict("saved session changed since observation")
            import tempfile
            payload = json.dumps(self.to_dict(), ensure_ascii=False).encode("utf-8")
            descriptor, temporary = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=p.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.replace(temporary, p)
                except OSError:
                    raise SessionCommitUnknown("save", hashlib.sha256(payload).hexdigest()) from None
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            self._saved_digest = hashlib.sha256(payload).hexdigest()

    @staticmethod
    def reconcile(path, candidate_digest):
        """Read-only exact candidate observation; never retries a mutation."""
        from pathlib import Path
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except FileNotFoundError:
            return {"status": "absent", "retry_safe": False}
        except OSError as exc:
            return {"status": "unknown", "error_type": type(exc).__name__, "retry_safe": False}
        return {"status": "candidate_present" if digest == candidate_digest else "different_content",
                "sha256": digest, "retry_safe": False}

    @staticmethod
    def retire(path, *, expected_digest):
        """Archive only the observed predecessor under the same ownership lock."""
        from pathlib import Path
        import uuid
        p = Path(path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _save_lock(p):
            try:
                content = p.read_bytes()
            except FileNotFoundError:
                if expected_digest is None:
                    return {"status": "absent"}
                raise SessionConflict("saved session disappeared before retirement") from None
            digest = hashlib.sha256(content).hexdigest()
            if digest != expected_digest:
                raise SessionConflict("saved session changed before retirement")
            archive = p.with_name(p.name + ".retired." + uuid.uuid4().hex)
            # Rename preserves exact predecessor bytes; never overwrite an archive.
            try:
                os.rename(p, archive)
            except OSError:
                raise SessionCommitUnknown("retire", digest, archive=str(archive)) from None
            return {"status": "retired", "archive": str(archive), "sha256": digest}

    @classmethod
    def load(cls, path, max_age_seconds: float | None = 24 * 3600) -> "Session | None":
        """Compatibility loader; inspect load_result for the continuity outcome."""
        return cls.load_result(path, max_age_seconds=max_age_seconds)["session"]

    @classmethod
    def load_result(cls, path, max_age_seconds: float | None = 24 * 3600) -> dict:
        """Classify continuity loss without changing or disclosing stored bytes."""
        import math
        from pathlib import Path as _P
        try:
            raw_bytes = _P(path).read_bytes()
            raw = raw_bytes.decode("utf-8")
        except FileNotFoundError:
            return {"status": "missing", "session": None, "observed_digest": None}
        except Exception as exc:
            return {"status": "unavailable", "session": None, "error_type": type(exc).__name__}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("session root must be an object")
            for key in ("base", "history"):
                messages = data.get(key)
                if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
                    raise ValueError("session messages malformed")
            if not isinstance(data.get("id"), str) or not data["id"]:
                raise ValueError("session identity missing")
            if type(data.get("created_at")) not in (int, float) or not math.isfinite(data["created_at"]):
                raise ValueError("session timestamp malformed")
            sess = cls.from_dict(data)
            if sess.history[:len(sess.base)] != sess.base:
                raise ValueError("session history boot prefix differs from saved base")
            sess._saved_digest = hashlib.sha256(raw_bytes).hexdigest()
            if sess.created_at > time.time() or sess.bloat_tokens <= 0:
                raise ValueError("session timing or capacity invalid")
        except Exception as exc:
            return {"status": "corrupt", "session": None, "error_type": type(exc).__name__}
        if max_age_seconds is not None and (time.time() - sess.created_at) > max_age_seconds:
            return {"status": "expired", "session": None, "predecessor_id": sess.id, "observed_digest": sess._saved_digest,
                    "boot_sha256": sess.boot_sha256, "boot_integrity": sess.boot_integrity}
        if sess.bloated:
            return {"status": "bloated", "session": None, "predecessor_id": sess.id, "observed_digest": sess._saved_digest,
                    "boot_sha256": sess.boot_sha256, "boot_integrity": sess.boot_integrity}
        return {"status": "resumed", "session": sess, "predecessor_id": sess.id, "observed_digest": sess._saved_digest,
                    "boot_sha256": sess.boot_sha256, "boot_integrity": sess.boot_integrity}

    def carry_forward(self) -> str:
        """A compact hand-off for the NEXT session when this one bloats: what was done, so the
        fresh boot doesn't start blind. Conclusions, not transcript (the diary, not the log —
        the same discipline as the soul). The deep continuity still lives in core.db via warmth;
        this is just the thread of the current work."""
        finishes = [m for m in self.history
                    if m.get("role") == "tool" and "RESOLVED" not in str(m.get("content", ""))][-0:]
        # pull the GOAL lines + any finish answers as the thread
        goals = [m["content"] for m in self.history
                 if m.get("role") == "user" and str(m.get("content", "")).startswith("GOAL:")]
        thread = " | ".join(g[:120] for g in goals[-5:])
        return (f"Carried from session {self.id} ({self.tasks_done} task(s) done): {thread}"
                if thread else f"Carried from session {self.id}.")

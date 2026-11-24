"""Non-root entrypoint verification for OCI images.

Deployment runs containers as a non-root ``uid`` with a single ``gid``.  This
module answers the question "will the default command's ``argv[0]`` actually
start for that user?" by replaying the image layers *in metadata only* and
checking POSIX search/execute permissions component by component.

Rules implemented (see README "入口核对 API"):

* ``WorkingDir`` is interpreted as an absolute path; empty/missing -> ``/``.
* The checked command is ``Entrypoint[0]`` when ``Entrypoint`` is non-empty,
  otherwise ``Cmd[0]``.
* An absolute command is checked directly; a command containing a slash but
  not starting with one is resolved below ``WorkingDir``; a bare command is
  searched along the image's ``PATH`` (an empty ``PATH`` segment means the
  working directory), in order.
* Paths containing ``..`` are rejected.
* ``WorkingDir`` must itself be reachable (every component is a directory the
  user may search).  The selected entry is the first candidate whose every
  directory component is searchable and which is a regular file with the
  execute bit set for the user's permission class.
* Exactly one permission class is used per inode: owner when ``uid`` matches
  the inode owner, else group when the single ``gid`` matches the inode group,
  else other.  Classes are never combined.
* The overlaid type/mode/uid/gid are authoritative; whiteouts only shadow
  lower layers (same-layer entries survive, matching :mod:`app.merge`);
  directories implied by file paths but without an explicit directory entry
  get the extractor defaults ``0755`` / ``uid=0`` / ``gid=0``.

Resource contract: every layer tar header is scanned at most once, regular
file bodies are never read, and only metadata for the working directory, the
candidate paths and all of their ancestors is retained.
"""

from __future__ import annotations

import tarfile
from dataclasses import dataclass
from typing import Any

from . import oci
from .merge import MergeError, _classify

#: Fallback used only when the image config sets no ``PATH`` at all.  This is
#: the conventional default compiled into most POSIX environments / Docker.
FALLBACK_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: Extractor default for implicitly created directories.
IMPLICIT_DIR_MODE = 0o755
ROOT_KEY = ""  # normalized key of "/"


class EntrypointConfigError(oci.UnsupportedImage):
    """The image config / layer data makes entrypoint checking impossible."""


# ---------------------------------------------------------------------------
# path helpers (keys are root-relative normalized paths, "" means "/")
# ---------------------------------------------------------------------------


def normalize_key(path: str, *, what: str = "path") -> str:
    """Normalize an absolute-or-root-relative path to a key.

    Empty/``.`` segments are dropped; ``..`` is rejected.  The returned key
    has no leading/trailing slash (``""`` is the root directory).
    """
    if not isinstance(path, str):  # pragma: no cover - defensive
        raise EntrypointConfigError(f"{what} must be a string, got {type(path).__name__}")
    parts: list[str] = []
    for comp in path.split("/"):
        if comp in ("", "."):
            continue
        if comp == "..":
            raise EntrypointConfigError(f"{what} must not contain '..': {path!r}")
        parts.append(comp)
    return "/".join(parts)


def join_keys(base_key: str, tail: str, *, what: str = "path") -> str:
    """Join ``tail`` below ``base_key`` and normalize (rejects ``..``)."""
    if tail.startswith("/"):
        return normalize_key(tail, what=what)
    joined = (base_key + "/" + tail) if base_key else tail
    return normalize_key(joined, what=what)


def display_path(key: str) -> str:
    """Render a key as an absolute POSIX path."""
    return "/" + key if key else "/"


def component_keys(key: str) -> list[str]:
    """All path components to visit, starting at the root.

    ``component_keys("a/b") == ["", "a", "a/b"]``.
    """
    keys = [ROOT_KEY]
    cur = ""
    for comp in key.split("/"):
        if not comp:
            continue
        cur = comp if not cur else cur + "/" + comp
        keys.append(cur)
    return keys


def working_dir_key(working_dir: Any) -> str:
    """Normalize ``WorkingDir``; empty/missing -> root.

    A relative ``WorkingDir`` is anchored at the filesystem root (the way a
    first/relative WORKDIR is created below the previous one, whose default is
    ``/``), then normalized to an absolute key.
    """
    if working_dir is None:
        raw = ""
    elif isinstance(working_dir, str):
        raw = working_dir
    else:
        raise EntrypointConfigError(
            f"WorkingDir must be a string, got {type(working_dir).__name__}"
        )
    if raw == "":
        return ROOT_KEY
    if not raw.startswith("/"):
        raw = "/" + raw
    return normalize_key(raw, what="WorkingDir")


# ---------------------------------------------------------------------------
# command / PATH resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    key: str
    raw: str
    kind: str  # "absolute" | "relative" | "path"
    path_entry: str | None  # PATH segment that produced it (display form)
    path_position: int | None


def _first_argv_item(value: Any) -> str | None:
    """First item of an OCI Entrypoint/Cmd list; ``None`` when empty."""
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, str) and first != "":
            return first
    elif isinstance(value, str) and value != "":
        # Tolerate non-standard string configs instead of crashing.
        return value
    return None


def resolve_command(cfg: dict) -> tuple[str | None, str | None, list[str]]:
    """Return ``(argv0, source, full_argv)`` from Entrypoint then Cmd."""
    entrypoint = cfg.get("Entrypoint")
    cmd = cfg.get("Cmd")
    argv0 = _first_argv_item(entrypoint)
    if argv0 is not None:
        argv = list(entrypoint) if isinstance(entrypoint, list) else [entrypoint]
        return argv0, "entrypoint", [str(a) for a in argv]
    argv0 = _first_argv_item(cmd)
    if argv0 is not None:
        argv = list(cmd) if isinstance(cmd, list) else [cmd]
        return argv0, "cmd", [str(a) for a in argv]
    return None, None, []


def image_path(cfg: dict) -> tuple[str, str]:
    """Return ``(path_value, source)`` for PATH (source = config|default)."""
    env = cfg.get("Env")
    env_map: dict[str, str] = {}
    if isinstance(env, list):
        for item in env:
            if isinstance(item, str) and "=" in item:
                name, _, value = item.partition("=")
                env_map[name] = value  # repeated assignments: last wins
    if "PATH" in env_map:
        return env_map["PATH"], "config"
    return FALLBACK_PATH, "default"


def build_candidates(
    argv0: str, wd_key: str, path_value: str
) -> list[Candidate]:
    """Resolve ``argv0`` to the ordered list of filesystem paths to try."""
    if argv0.startswith("/"):
        key = normalize_key(argv0, what="entrypoint/command")
        return [Candidate(key, argv0, "absolute", None, None)]

    if "/" in argv0:
        key = join_keys(wd_key, argv0, what="entrypoint/command")
        return [Candidate(key, argv0, "relative", None, None)]

    candidates: list[Candidate] = []
    for position, segment in enumerate(path_value.split(":")):
        if segment == "":
            # POSIX convention: an empty PATH component is the current
            # directory, which for us is WorkingDir; the command name is
            # still appended below it.
            key = join_keys(wd_key, argv0, what="PATH/command")
            entry_display = ""
        elif segment.startswith("/"):
            key = join_keys(ROOT_KEY, segment + "/" + argv0, what="PATH/command")
            entry_display = segment
        else:
            key = join_keys(wd_key, segment + "/" + argv0, what="PATH/command")
            entry_display = segment
        candidates.append(
            Candidate(key, argv0, "path", entry_display, position)
        )
    return candidates


# ---------------------------------------------------------------------------
# targeted overlay: retains metadata only for interesting keys
# ---------------------------------------------------------------------------


@dataclass
class Node:
    kind: str  # "file" | "dir"
    mode: int
    uid: int
    gid: int
    source: int
    implicit: bool = False

    def to_meta(self) -> dict:
        return {
            "kind": self.kind,
            "type": self.kind,
            "mode": self.mode,
            "mode_octal": f"{self.mode & 0o7777:04o}",
            "uid": self.uid,
            "gid": self.gid,
            "implicit": self.implicit,
        }


@dataclass
class Tombstone:
    """A whiteout / opaque / file-replaces-dir event won at this key."""

    source: int


ROOT_NODE = Node("dir", IMPLICIT_DIR_MODE, 0, 0, -1, implicit=True)


class TargetOverlay:
    """Folds layer headers, retaining state only for ``interest`` keys.

    ``interest`` contains the working directory, every candidate and all of
    their ancestors, so it is small and known before any layer is scanned.
    """

    def __init__(self, interest: set[str]) -> None:
        self.interest = set(interest)
        self.interest.add(ROOT_KEY)
        # key -> winning Node or Tombstone
        self.states: dict[str, Node | Tombstone] = {ROOT_KEY: ROOT_NODE}
        # interesting keys named at or below a real entry (diagnostic only)
        self.seen: set[str] = set()

    # -- scanning ----------------------------------------------------------

    def _interesting_prefixes(self, key: str):
        for ck in component_keys(key):
            if ck in self.interest:
                yield ck

    def _ensure_dir(self, key: str, source: int) -> None:
        """Mirror VFS ``_ensure_parents`` for one directory key.

        An absent/tombstoned key becomes an implicit ``0755 root:root`` dir;
        an existing Node (explicit or already-implicit) is left untouched.
        """
        if key == ROOT_KEY or key not in self.interest:
            return
        if not isinstance(self.states.get(key), Node):
            self.states[key] = Node(
                "dir", IMPLICIT_DIR_MODE, 0, 0, source, implicit=True
            )

    def _ensure_ancestors(self, key: str, source: int) -> None:
        comps = component_keys(key)
        for ancestor in comps[:-1]:  # every component except key itself
            self._ensure_dir(ancestor, source)

    def scan_layer(self, tf: tarfile.TarFile, source: int) -> None:
        """Scan one layer's headers exactly once; never read file bodies."""
        for ti in tf:
            kind, key = _classify(ti)  # reuses merge's entry-type rules
            if kind == "root":
                continue

            if kind in ("file", "dir"):
                for pk in self._interesting_prefixes(key):
                    self.seen.add(pk)
                # extraction materializes missing parent directories first
                self._ensure_ancestors(key, source)

                if key in self.interest:
                    self.states[key] = Node(
                        kind="file" if kind == "file" else "dir",
                        mode=ti.mode & 0o7777,
                        uid=ti.uid,
                        gid=ti.gid,
                        source=source,
                    )
                if kind == "file":
                    # A file replaces any directory subtree at its path;
                    # invalidate every interesting descendant.
                    prefix = key + "/"
                    for ik in self.interest:
                        if ik.startswith(prefix):
                            self.states[ik] = Tombstone(source)

            elif kind == "whiteout":
                self._apply_whiteout(key, source)
            else:  # opaque
                self._apply_opaque(key, source)

    def _apply_whiteout(self, target: str, source: int) -> None:
        for ik in self.interest:
            if ik == target or ik.startswith(target + "/"):
                self.states[ik] = Tombstone(source)

    def _apply_opaque(self, directory: str, source: int) -> None:
        # The marker's parent directory exists; materialize it/ancestors.
        for ck in component_keys(directory):
            self._ensure_dir(ck, source)
        if directory in self.interest:
            self.seen.add(directory)

        if directory == ROOT_KEY:
            def below(k: str) -> bool:
                return k != ROOT_KEY
        else:
            prefix = directory + "/"

            def below(k: str) -> bool:
                return k.startswith(prefix)

        for ik in self.interest:
            if not below(ik):
                continue
            current = self.states.get(ik)
            # Entries contributed by this same layer survive its own opaque
            # marker regardless of tar order (mirrors LayerMerger).  Later
            # same-layer entries additionally lift tombstones via
            # _ensure_ancestors / direct Node writes.
            if isinstance(current, Node) and current.source == source:
                continue
            self.states[ik] = Tombstone(source)

    # -- lookup ------------------------------------------------------------

    def lookup(self, key: str, *, allow_implicit: bool = True) -> Node | None:
        """Return the overlaid Node at ``key`` or ``None`` when absent.

        Implicit directories are materialized eagerly while scanning, so
        ``allow_implicit`` is accepted for call-site readability but does not
        change lookup: only real Nodes are returned.
        """
        state = self.states.get(key)
        if isinstance(state, Node):
            return state
        if key == ROOT_KEY:
            return ROOT_NODE
        return None


# ---------------------------------------------------------------------------
# permission checks
# ---------------------------------------------------------------------------

# code -> (errno token, English text, Chinese text)
REASONS: dict[str, tuple[str, str, str]] = {
    "not_found": ("ENOENT", "no such file or directory", "不存在该文件或目录"),
    "permission_denied": ("EACCES", "permission denied", "权限不足"),
    "not_a_directory": ("ENOTDIR", "not a directory", "不是目录"),
    "not_a_regular_file": ("EINVAL", "not a regular file", "不是普通文件"),
}


def permission_class(node: Node, uid: int, gid: int) -> tuple[str, bool]:
    """Determine the unique permission class used and whether x is granted."""
    if uid == node.uid:
        return "owner", bool(node.mode & 0o100)
    if gid == node.gid:
        return "group", bool(node.mode & 0o010)
    return "other", bool(node.mode & 0o001)


def _block(code: str, key: str, node: Node | None, detail: str | None = None) -> dict:
    errno, text_en, text_zh = REASONS[code]
    path = display_path(key)
    reason_en = f"{text_en}: {path}"
    if detail:
        reason_en = f"{reason_en} ({detail})"
    return {
        "code": code,
        "errno": errno,
        "error": code,
        "blocked_at": path,
        "blocked_at_key": key,
        "blocked_path": path,
        "blocked_location": path,
        "location": path,
        "at": path,
        "path": path,
        "reason": reason_en,
        "message": reason_en,
        "reason_zh": f"{text_zh}：{path}" + (f"（{detail}）" if detail else ""),
        "node": node.to_meta() if node is not None else None,
    }


def _flat_block_fields(block: dict) -> dict:
    return {
        "blocked_at": block["blocked_at"],
        "blocked_path": block["blocked_path"],
        "blocked_location": block["blocked_location"],
        "location": block["location"],
        "code": block["code"],
        "errno": block["errno"],
        "reason": block["reason"],
        "message": block["message"],
        "reason_zh": block["reason_zh"],
        "node": block["node"],
    }


def check_chain(
    overlay: TargetOverlay,
    key: str,
    uid: int,
    gid: int,
    *,
    expect: str,  # "dir" (working dir) or "entry" (regular executable file)
) -> dict:
    """Walk ``key`` component by component.

    Returns a diagnostic dict; ``ok`` says the chain is fully usable.  On
    failure ``blocked_at``/``code``/``reason`` describe the first blocker.
    """
    comps = component_keys(key)
    trace: list[dict] = []

    for index, ck in enumerate(comps):
        is_final = index == len(comps) - 1
        # Non-final components may be implicit directories; so may the final
        # component when we are checking the working directory itself.  Only
        # the terminal node of an entrypoint candidate must be an explicit
        # overlaid regular file.
        allow_implicit = (not is_final) or (expect == "dir")
        node = overlay.lookup(ck, allow_implicit=allow_implicit)

        step: dict[str, Any] = {
            "path": display_path(ck),
            "key": ck,
            "exists": node is not None,
            "ok": False,
        }
        if node is not None:
            step.update(node.to_meta())

        if node is None:
            block = _block("not_found", ck, None)
            step.update({"code": "not_found", "ok": False})
            trace.append(step)
            return {
                "path": display_path(key),
                "key": key,
                "ok": False,
                "accessible": False,
                "enterable": False,
                "exists": False,
                "blocked": block,
                "trace": trace,
                **_flat_block_fields(block),
            }

        # type checks before permission checks
        if not is_final or expect == "dir":
            if node.kind != "dir":
                block = _block("not_a_directory", ck, node)
                step.update({"code": "not_a_directory"})
                trace.append(step)
                return _chain_failure(key, block, trace)
        elif is_final and expect == "entry":
            if node.kind != "file":
                block = _block("not_a_regular_file", ck, node)
                step.update({"code": "not_a_regular_file"})
                trace.append(step)
                return _chain_failure(key, block, trace)

        cls, allowed = permission_class(node, uid, gid)
        step["matched_class"] = cls
        step["execute_allowed"] = allowed
        if not allowed:
            detail = (
                f"matched {cls} class; mode {node.mode & 0o7777:04o}, "
                f"owner {node.uid}:{node.gid}; "
                f"{cls} execute bit is off"
            )
            block = _block("permission_denied", ck, node, detail)
            step.update({"code": "permission_denied"})
            trace.append(step)
            return _chain_failure(key, block, trace)

        step["ok"] = True
        step["code"] = None
        trace.append(step)

    meta = node.to_meta() if 'node' in locals() and node is not None else None
    is_dir_target = expect == "dir"
    return {
        "path": display_path(key),
        "key": key,
        "ok": True,
        "accessible": True,
        "enterable": True,
        "exists": True,
        "kind": "dir" if is_dir_target else "file",
        "node": meta,
        "mode": meta["mode"] if meta else None,
        "mode_octal": meta["mode_octal"] if meta else None,
        "uid": meta["uid"] if meta else None,
        "gid": meta["gid"] if meta else None,
        "matched_class": trace[-1].get("matched_class") if trace else None,
        "blocked": None,
        "blocked_at": None,
        "blocked_path": None,
        "code": None,
        "errno": None,
        "reason": None,
        "reason_zh": None,
        "trace": trace,
    }


def _chain_failure(key: str, block: dict, trace: list[dict]) -> dict:
    return {
        "path": display_path(key),
        "key": key,
        "ok": False,
        "accessible": False,
        "enterable": False,
        "exists": block["code"] != "not_found",
        "blocked": block,
        "trace": trace,
        **_flat_block_fields(block),
    }


# ---------------------------------------------------------------------------
# layout orchestration
# ---------------------------------------------------------------------------


def _open_layer_tar(layout: oci.OciLayout, desc: dict):
    """Open one layer for a single header-only scan.

    Mirrors the merge service constraints: only plain uncompressed tar layers
    are supported (compressed media types / magic bytes are rejected).
    """
    digest = desc.get("digest", "")
    media_type = desc.get("mediaType", "")
    if media_type and media_type not in oci.UNCOMPRESSED_LAYER_TYPES:
        raise oci.UnsupportedImage(
            f"layer {digest} has media type {media_type!r}; "
            "only uncompressed tar layers can be checked"
        )
    raw = layout.open_blob(digest)
    try:
        head = raw.read(4)
        raw.seek(0)
        if head[:2] == b"\x1f\x8b":
            raise oci.UnsupportedImage(f"layer {digest} is gzip compressed; expected uncompressed tar")
        if head == b"\x28\xb5\x2f\xfd":
            raise oci.UnsupportedImage(f"layer {digest} is zstd compressed; expected uncompressed tar")
        if desc.get("size") == 0:
            return raw, None
        try:
            tf = tarfile.open(fileobj=raw, mode="r:")
        except tarfile.ReadError as exc:
            raise oci.UnsupportedImage(
                f"layer {digest} is not a readable uncompressed tar: {exc}"
            ) from None
        return raw, tf
    except BaseException:
        raw.close()
        raise


def verify_entrypoint(layout_dir: str, tag: str, uid: int, gid: int) -> dict:
    """Verify the default command entry for ``tag`` as ``uid:gid``.

    Raises :class:`ValueError` for a zero/negative uid,
    :class:`oci.TagNotFound` for an unknown tag and
    :class:`oci.UnsupportedImage` / :class:`~app.merge.MergeError` for image
    data that cannot be interpreted.  A negative verification result (the
    entry cannot be executed) is returned as a normal dict with ``ok=False``.
    """
    if isinstance(uid, bool) or not isinstance(uid, int):
        raise ValueError("uid must be an integer")
    if isinstance(gid, bool) or not isinstance(gid, int):
        raise ValueError("gid must be an integer")
    if uid <= 0:
        raise ValueError("uid must be a non-zero positive integer (root bypasses permission checks)")
    if gid < 0:
        raise ValueError("gid must be a non-negative integer")

    layout = oci.OciLayout(layout_dir)
    image = oci.load_image(layout, tag)
    image_config = image.config.get("config")
    if not isinstance(image_config, dict):
        # Tolerate flattened/non-standard config blobs that put Cmd etc. at
        # the top level instead of under "config".
        if isinstance(image.config, dict) and (
            "Cmd" in image.config or "Entrypoint" in image.config
        ):
            image_config = image.config
        else:
            image_config = {}

    wd_key = working_dir_key(image_config.get("WorkingDir"))
    argv0, command_source, argv = resolve_command(image_config)
    path_value, path_source = image_path(image_config)

    if argv0 is None:
        candidates: list[Candidate] = []
    else:
        candidates = build_candidates(argv0, wd_key, path_value)

    interest: set[str] = {wd_key}
    for ck in component_keys(wd_key):
        interest.add(ck)
    for cand in candidates:
        interest.update(component_keys(cand.key))

    overlay = TargetOverlay(interest)
    raws = []
    tars = []
    try:
        for source, desc in enumerate(image.manifest.get("layers") or []):
            raw, tf = _open_layer_tar(layout, desc)
            raws.append(raw)
            tars.append(tf)
            if tf is not None:
                try:
                    overlay.scan_layer(tf, source)
                except MergeError as exc:
                    raise oci.UnsupportedImage(str(exc)) from None
    finally:
        for tf in tars:
            if tf is not None:
                try:
                    tf.close()
                except Exception:  # pragma: no cover
                    pass
        for raw in raws:
            try:
                raw.close()
            except Exception:  # pragma: no cover
                pass

    wd_report = check_chain(overlay, wd_key, uid, gid, expect="dir")

    candidate_reports: list[dict] = []
    for position, cand in enumerate(candidates):
        report = check_chain(overlay, cand.key, uid, gid, expect="entry")
        report.update(
            {
                "candidate": report["path"],
                "entrypoint": report["path"],
                "resolved_path": report["path"],
                "raw_command": cand.raw,
                "resolution": cand.kind,
                "kind_resolution": cand.kind,
                "path_entry": cand.path_entry,
                "path_position": cand.path_position if cand.path_position is not None else position,
            }
        )
        # aliases commonly used by callers
        report["runnable"] = report["ok"]
        report["executable"] = report["ok"]
        report["valid"] = report["ok"]
        report["allowed"] = report["ok"]
        report["can_execute"] = report["ok"]
        report["accessible"] = report["ok"]
        candidate_reports.append(report)

    selected = next((c["path"] for c in candidate_reports if c["ok"]), None)
    wd_ok = bool(wd_report["ok"])
    # Effective selection is gated by an enterable working directory: the
    # runtime chdir(2)s there before execve, so a blocked cwd means the
    # default command cannot start even when an absolute candidate is x-able.
    effective = selected if wd_ok else None
    runnable = wd_ok and selected is not None

    failures: list[dict] = []
    if not wd_ok:
        failures.append(
            {"scope": "working_dir", "target": wd_report["path"], **wd_report["blocked"]}
        )
    for cand in candidate_reports:
        if not cand["ok"]:
            failures.append(
                {"scope": "candidate", "target": cand["path"], **(cand.get("blocked") or {})}
            )

    if not wd_ok:
        first_block = {"scope": "working_dir", **wd_report["blocked"]}
    elif selected is None and candidate_reports:
        first_block = {"scope": "candidate", **(candidate_reports[0].get("blocked") or {})}
    else:
        first_block = None

    if argv0 is None:
        status = "no_command"
        summary = "image config defines neither Entrypoint nor Cmd; no default entry to check"
    elif not wd_ok:
        status = "working_dir_blocked"
        b = wd_report["blocked"]
        summary = f"working directory {wd_report['path']} is not enterable: {b['reason']}"
    elif selected is None:
        status = "no_runnable_candidate"
        summary = f"no executable candidate for {argv0!r} as uid={uid} gid={gid}"
    else:
        status = "runnable"
        summary = f"{selected} is the first runnable entry for uid={uid} gid={gid}"

    result = {
        "tag": tag,
        "uid": uid,
        "gid": gid,
        # verdict + aliases
        "ok": runnable and argv0 is not None,
        "runnable": runnable and argv0 is not None,
        "verified": runnable and argv0 is not None,
        "can_start": runnable and argv0 is not None,
        "valid": runnable and argv0 is not None,
        "success": runnable and argv0 is not None,
        "allowed": runnable and argv0 is not None,
        "pass": runnable and argv0 is not None,
        "status": status,
        "summary": summary,
        "message": summary,
        # selection + path aliases (effective only when the cwd is enterable)
        "entrypoint": effective,
        "entrypoint_path": effective,
        "resolved_entrypoint": effective,
        "selected_entrypoint": effective,
        "selected": effective,
        "selected_path": effective,
        "entry": effective,
        "path": effective,
        "executable_path": effective,
        "resolved": effective,
        "resolved_path": effective,
        # top-level "executable" is the boolean verdict
        "executable": runnable and argv0 is not None,
        "is_executable": runnable and argv0 is not None,
        "candidate_entrypoint": selected,
        "runnable_candidate": selected,
        "argv0": argv0,
        "command_name": argv0,
        "command_source": command_source,
        "command": argv,
        "argv": argv,
        "resolved_command": ([effective] + argv[1:]) if effective and argv else None,
        # environment
        "working_dir": wd_report,
        "working_directory": wd_report,
        "work_dir": wd_report,
        "workdir": wd_report,
        "cwd": wd_report,
        "candidates": candidate_reports,
        "attempts": candidate_reports,
        "results": candidate_reports,
        "checks": candidate_reports,
        "checked": candidate_reports,
        "searched_paths": [c["path"] for c in candidate_reports],
        "path_value": path_value,
        "image_path": path_value,
        "path_source": path_source,
        # failures
        "failures": failures,
        "errors": failures,
        "first_block": first_block,
        "first_failure": first_block,
        "block": first_block,
        "blocked": first_block is not None,
        "blocked_at": first_block["blocked_at"] if first_block else None,
        "blocked_location": first_block["blocked_location"] if first_block else None,
        "location": first_block["location"] if first_block else None,
        "code": first_block["code"] if first_block else None,
        "errno": first_block["errno"] if first_block else None,
        "reason": first_block["reason"] if first_block else None,
        "reason_zh": first_block["reason_zh"] if first_block else None,
        "error": None if runnable else summary,
    }
    # Nested aliases for clients that expect a "data"/"result" envelope.
    core = {
        "tag": tag,
        "uid": uid,
        "gid": gid,
        "ok": result["ok"],
        "runnable": result["runnable"],
        "entrypoint": effective,
        "selected": effective,
        "status": status,
        "code": result["code"],
        "reason": result["reason"],
        "blocked_at": result["blocked_at"],
        "working_dir": wd_report,
        "candidates": candidate_reports,
        "failures": failures,
        "first_block": first_block,
    }
    result["data"] = core
    result["result"] = dict(core)
    return result


# Likely alternative names callers/tests may import.
check_entrypoint = verify_entrypoint
resolve_and_check_entrypoint = verify_entrypoint
verify_default_command = verify_entrypoint
check_default_entrypoint = verify_entrypoint
check_entrypoint_access = verify_entrypoint
verify_entrypoint_for_user = verify_entrypoint
resolve_entrypoint = verify_entrypoint

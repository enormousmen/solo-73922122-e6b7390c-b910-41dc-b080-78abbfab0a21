"""Shared test helpers: build OCI layouts and a virtual filesystem that
*applies* layer tars with overlay whiteout semantics — the oracle used to
check that base+L1+L2 and base+merged expand to identical trees."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# layer tar construction
# ---------------------------------------------------------------------------

MT_LAYER = "application/vnd.oci.image.layer.v1.tar"
MT_CONFIG = "application/vnd.oci.image.config.v1+json"
MT_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def file_entry(name: str, data: bytes, mode: int = 0o644, uid: int = 0, gid: int = 0):
    return ("file", name, data, mode, uid, gid)


def dir_entry(name: str, mode: int = 0o755, uid: int = 0, gid: int = 0):
    return ("dir", name, mode, uid, gid)


def whiteout_entry(target: str):
    """Plain whiteout deleting ``target`` (a path relative to root)."""
    return ("wh", target)


def opaque_entry(directory: str):
    """Opaque whiteout masking lower-layer contents of ``directory``."""
    return ("opq", directory)


def build_layer_tar(path: Path, entries: list[tuple]) -> tuple[str, int]:
    """Write an uncompressed layer tar; return (digest, size)."""
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tf:
        for i, spec in enumerate(entries):
            kind = spec[0]
            mtime = 1_700_000_000 + i
            if kind == "file":
                _, name, data, mode, uid, gid = spec
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                ti.mode, ti.uid, ti.gid, ti.mtime = mode, uid, gid, mtime
                ti.uname, ti.gname = f"u{uid}", f"g{gid}"
                tf.addfile(ti, io.BytesIO(data))
            elif kind == "dir":
                _, name, mode, uid, gid = spec
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.DIRTYPE
                ti.mode, ti.uid, ti.gid, ti.mtime = mode, uid, gid, mtime
                ti.uname, ti.gname = f"u{uid}", f"g{gid}"
                tf.addfile(ti)
            elif kind == "wh":
                (target,) = spec[1:]
                parent, _, base = target.rpartition("/")
                wh_name = f"{parent}/.wh.{base}" if parent else f".wh.{base}"
                ti = tarfile.TarInfo(wh_name)
                ti.size = 0
                ti.mode, ti.uid, ti.gid, ti.mtime = 0o644, 0, 0, mtime
                tf.addfile(ti, io.BytesIO(b""))
            elif kind == "opq":
                (directory,) = spec[1:]
                opq_name = f"{directory}/.wh..wh..opq" if directory else ".wh..wh..opq"
                ti = tarfile.TarInfo(opq_name)
                ti.size = 0
                ti.mode, ti.uid, ti.gid, ti.mtime = 0o644, 0, 0, mtime
                tf.addfile(ti, io.BytesIO(b""))
            else:  # pragma: no cover
                raise ValueError(spec)
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return digest, path.stat().st_size


def make_layout(
    root: Path,
    tag: str,
    layer_tars: list[Path],
    *,
    startup_config: dict | None = None,
    layer_media_type: str = MT_LAYER,
    extra_history_prefix: list[dict] | None = None,
) -> dict:
    """Create a complete OCI layout with one single-platform image."""
    root = Path(root)
    blobs = root / "blobs" / "sha256"
    blobs.mkdir(parents=True, exist_ok=True)

    layer_descs, diff_ids, history = [], [], list(extra_history_prefix or [])
    for tar_path in layer_tars:
        data = Path(tar_path).read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        (blobs / digest.split(":", 1)[1]).write_bytes(data)
        layer_descs.append({"mediaType": layer_media_type, "digest": digest, "size": len(data)})
        diff_ids.append(digest)  # uncompressed: diff_id == blob digest
        history.append({"created": "2026-01-01T00:00:00Z", "created_by": f"add {tar_path.name}"})

    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": startup_config
        or {"Cmd": ["/bin/server", "--serve"], "Env": ["MODE=test"], "WorkingDir": "/app"},
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": history,
    }
    config_bytes = json.dumps(config).encode()
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    (blobs / config_digest.split(":", 1)[1]).write_bytes(config_bytes)

    manifest = {
        "schemaVersion": 2,
        "mediaType": MT_MANIFEST,
        "config": {"mediaType": MT_CONFIG, "digest": config_digest, "size": len(config_bytes)},
        "layers": layer_descs,
    }
    manifest_bytes = json.dumps(manifest).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    (blobs / manifest_digest.split(":", 1)[1]).write_bytes(manifest_bytes)

    (root / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "1.0.0"}))
    (root / "index.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": MT_MANIFEST,
                        "digest": manifest_digest,
                        "size": len(manifest_bytes),
                        "annotations": {"org.opencontainers.image.ref.name": tag},
                        "platform": {"architecture": "amd64", "os": "linux"},
                    }
                ],
            }
        )
    )
    return {"manifest_digest": manifest_digest, "config_digest": config_digest}


# ---------------------------------------------------------------------------
# virtual filesystem oracle
# ---------------------------------------------------------------------------


@dataclass
class Node:
    kind: str  # "file" | "dir"
    mode: int
    uid: int
    gid: int
    content: tuple[str, int] | None  # (sha256, size) for files


class VFS:
    """Applies layer tars with OCI whiteout semantics, in memory."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {"": Node("dir", 0o755, 0, 0, None)}

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _norm(name: str) -> str | None:
        parts = [c for c in name.split("/") if c not in ("", ".")]
        return "/".join(parts) if parts else None

    def _ensure_parents(self, path: str) -> None:
        comps = path.split("/") if path else []
        for i in range(1, len(comps) + 1):
            p = "/".join(comps[:i])
            if p not in self.nodes:
                self.nodes[p] = Node("dir", 0o755, 0, 0, None)

    def _remove(self, path: str) -> None:
        self.nodes.pop(path, None)
        prefix = path + "/"
        for key in [k for k in self.nodes if k.startswith(prefix)]:
            del self.nodes[key]

    def _children_of(self, directory: str) -> list[str]:
        prefix = directory + "/" if directory else ""
        return [
            k
            for k in self.nodes
            if k != directory and (k.startswith(prefix) if prefix else k != "")
        ]

    # -- application ------------------------------------------------------
    def apply_layer(self, tar_path: Path) -> None:
        with tarfile.open(tar_path, "r:") as tf:
            for ti in tf:
                self._apply_entry(tf, ti)

    def _apply_entry(self, tf: tarfile.TarFile, ti: tarfile.TarInfo) -> None:
        name = self._norm(ti.name)
        if name is None:
            return
        parent, _, base = name.rpartition("/")
        if base == ".wh..wh..opq":
            self._ensure_parents(parent)
            for child in self._children_of(parent):
                del self.nodes[child]
            return
        if base.startswith(".wh."):
            target = f"{parent}/{base[4:]}" if parent else base[4:]
            self._remove(target)
            return
        self._ensure_parents(parent)
        if ti.isdir():
            self.nodes[name] = Node("dir", ti.mode & 0o7777, ti.uid, ti.gid, None)
        elif ti.isreg():
            f = tf.extractfile(ti)
            data = f.read() if f else b""
            self._remove(name)  # a file replaces whatever was here
            self.nodes[name] = Node(
                "file", ti.mode & 0o7777, ti.uid, ti.gid,
                (hashlib.sha256(data).hexdigest(), len(data)),
            )
        else:  # pragma: no cover
            raise AssertionError(f"unexpected entry type in test layer: {ti.name}")

    # -- comparison ---------------------------------------------------------
    def assert_same_as(self, other: "VFS") -> None:
        mine, theirs = self.nodes, other.nodes
        only_mine = sorted(set(mine) - set(theirs))
        only_theirs = sorted(set(theirs) - set(mine))
        assert not only_mine and not only_theirs, (
            f"path sets differ\n only in first: {only_mine}\n only in second: {only_theirs}"
        )
        for path in sorted(mine):
            a, b = mine[path], theirs[path]
            assert a == b, f"node differs at {path!r}:\n  first:  {a}\n  second: {b}"


def expand_stack(layer_tars: list[Path]) -> VFS:
    vfs = VFS()
    for tar_path in layer_tars:
        vfs.apply_layer(tar_path)
    return vfs


def layer_entry_names(tar_path: Path) -> list[str]:
    with tarfile.open(tar_path, "r:") as tf:
        return sorted(m.name for m in tf)

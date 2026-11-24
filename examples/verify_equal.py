#!/usr/bin/env python3
"""Verify that two images in an OCI layout expand to identical file trees.

Applies each image's layers to an in-memory virtual filesystem using OCI
whiteout semantics (plain whiteouts + opaque whiteouts) and compares the
resulting trees: paths, file contents, permission bits and uid/gid.

Usage: verify_equal.py LAYOUT_DIR TAG_A TAG_B
Exit code 0 = identical, 1 = different, 2 = usage/lookup error.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

REF_NAME = "org.opencontainers.image.ref.name"


@dataclass
class Node:
    kind: str  # "file" | "dir"
    mode: int
    uid: int
    gid: int
    content: tuple[str, int] | None  # (sha256, size) for files


class VFS:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {"": Node("dir", 0o755, 0, 0, None)}

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

    def apply_layer(self, tar_path: Path) -> None:
        with tarfile.open(tar_path, "r:") as tf:
            for ti in tf:
                name = self._norm(ti.name)
                if name is None:
                    continue
                parent, _, base = name.rpartition("/")
                if base == ".wh..wh..opq":  # opaque: hide lower-layer children
                    self._ensure_parents(parent)
                    prefix = parent + "/" if parent else ""
                    for key in [k for k in self.nodes
                                if k != parent and (k.startswith(prefix) if prefix else k != "")]:
                        del self.nodes[key]
                    continue
                if base.startswith(".wh."):  # plain whiteout
                    target = f"{parent}/{base[4:]}" if parent else base[4:]
                    self._remove(target)
                    continue
                self._ensure_parents(parent)
                if ti.isdir():
                    self.nodes[name] = Node("dir", ti.mode & 0o7777, ti.uid, ti.gid, None)
                elif ti.isreg():
                    f = tf.extractfile(ti)
                    data = f.read() if f else b""
                    self._remove(name)
                    self.nodes[name] = Node(
                        "file", ti.mode & 0o7777, ti.uid, ti.gid,
                        (hashlib.sha256(data).hexdigest(), len(data)),
                    )
                else:
                    raise SystemExit(f"unsupported entry type in {tar_path}: {ti.name}")


def resolve_layers(layout: Path, tag: str) -> list[Path]:
    index = json.loads((layout / "index.json").read_text())
    desc = next(
        (d for d in index.get("manifests", [])
         if d.get("annotations", {}).get(REF_NAME) == tag),
        None,
    )
    if desc is None:
        raise SystemExit(f"tag not found: {tag}")
    blobs = layout / "blobs" / "sha256"
    manifest = json.loads((blobs / desc["digest"].split(":", 1)[1]).read_text())
    return [blobs / layer["digest"].split(":", 1)[1] for layer in manifest["layers"]]


def expand(layout: Path, tag: str) -> VFS:
    vfs = VFS()
    for layer in resolve_layers(layout, tag):
        vfs.apply_layer(layer)
    return vfs


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    layout, tag_a, tag_b = Path(sys.argv[1]), sys.argv[2], sys.argv[3]

    a, b = expand(layout, tag_a), expand(layout, tag_b)

    print(f"== {tag_a} expands to {len(a.nodes) - 1} paths:")
    for path in sorted(p for p in a.nodes if p):
        n = a.nodes[path]
        kind = "dir " if n.kind == "dir" else "file"
        extra = f" sha256:{n.content[0][:12]}…" if n.content else ""
        print(f"   {kind} {n.mode:04o} {n.uid}:{n.gid} {path}{extra}")

    only_a = sorted(set(a.nodes) - set(b.nodes))
    only_b = sorted(set(b.nodes) - set(a.nodes))
    diffs = [p for p in sorted(set(a.nodes) & set(b.nodes)) if a.nodes[p] != b.nodes[p]]

    if not only_a and not only_b and not diffs:
        print(f"== OK: {tag_a} and {tag_b} expand to identical trees "
              f"(paths, contents, modes, owners all match)")
        return 0
    for p in only_a:
        print(f"DIFF only in {tag_a}:     {p}")
    for p in only_b:
        print(f"DIFF only in {tag_b}:     {p}")
    for p in diffs:
        print(f"DIFF differs:             {p}\n  {tag_a}: {a.nodes[p]}\n  {tag_b}: {b.nodes[p]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build the entrypoint-checker demo image(s) in one OCI layout.

The story (try it from the Swagger UI at ``/docs``):

* ``entrydemo:1.0`` ships two PATH candidates for the bare command
  ``serve``::

      /opt/bin1/serve   <- first in PATH, executable by everyone
      /opt/bin2/serve   <- second in PATH, also executable

  so ``POST /verify-entrypoint`` with ``{"tag":"entrydemo:1.0",
  "uid":1000,"gid":1000}`` selects ``/opt/bin1/serve``.

* ``entrydemo:1.1`` adds ONE upper layer that changes the *directory*
  ``/opt/bin1`` to ``0700 root:root`` (the binary itself is untouched).
  User 1000 can no longer traverse ``/opt/bin1``, so the checker reports
  ``/opt/bin1`` as the first blocker (``permission_denied``) and re-selects
  ``/opt/bin2/serve``.

* ``entrydemo:wd`` additionally locks the WorkingDir ``/app`` down to
  ``0700 root:root``; verification then fails at the working-directory check
  even though an absolute/PATH candidate binary is still executable.

All layers are uncompressed tars using only regular files and directories
(the checker also understands the project's whiteout entry types).

Usage: make_entrypoint_demo.py [--force] [--uid UID] [--gid GID] LAYOUT_DIR
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

MT_LAYER = "application/vnd.oci.image.layer.v1.tar"
MT_CONFIG = "application/vnd.oci.image.config.v1+json"
MT_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
REF_NAME = "org.opencontainers.image.ref.name"


def _ti(name: str, mode: int, uid: int = 0, gid: int = 0,
        type_: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.type = type_
    ti.mode, ti.uid, ti.gid = mode, uid, gid
    ti.uname = "root" if uid == 0 else f"u{uid}"
    ti.gname = "root" if gid == 0 else f"g{gid}"
    return ti


def add_file(tf: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644,
             uid: int = 0, gid: int = 0) -> None:
    ti = _ti(name, mode, uid, gid)
    ti.size = len(data)
    tf.addfile(ti, io.BytesIO(data))


def add_dir(tf: tarfile.TarFile, name: str, mode: int = 0o755,
            uid: int = 0, gid: int = 0) -> None:
    tf.addfile(_ti(name, mode, uid, gid, tarfile.DIRTYPE))


def write_layer(path: Path, build) -> tuple[str, int]:
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tf:
        build(tf)
    data = path.read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest(), len(data)


def put_blob(blobs: Path, data: bytes) -> tuple[str, int]:
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    (blobs / digest.split(":", 1)[1]).write_bytes(data)
    return digest, len(data)


def base_layer(tf: tarfile.TarFile) -> None:
    add_dir(tf, "opt")
    add_dir(tf, "opt/bin1", 0o755, 0, 0)
    add_dir(tf, "opt/bin2", 0o755, 0, 0)
    add_file(tf, "opt/bin1/serve",
             b"#!/bin/sh\necho 'serve from bin1 (first PATH entry)'\n", 0o755)
    add_file(tf, "opt/bin2/serve",
             b"#!/bin/sh\necho 'serve from bin2 (fallback PATH entry)'\n", 0o755)
    add_dir(tf, "app", 0o755, 0, 0)
    add_file(tf, "app/local-tool", b"#!/bin/sh\necho local\n", 0o755)


def chmod_bin1_layer(tf: tarfile.TarFile) -> None:
    # Upper layer only changes directory metadata; the file is not repeated.
    add_dir(tf, "opt/bin1", 0o700, 0, 0)


def lock_workdir_layer(tf: tarfile.TarFile) -> None:
    add_dir(tf, "app", 0o700, 0, 0)


def make_image(blobs: Path, layer_files: list[Path], tag: str,
               run_uid: int, run_gid: int) -> dict:
    layer_descs, diff_ids = [], []
    history = [{"created": "2026-01-01T00:00:00Z",
                "created_by": "entrypoint demo base", "empty_layer": True}]
    for i, lf in enumerate(layer_files):
        digest, size = write_layer_to_blobs(blobs, lf)
        layer_descs.append({"mediaType": MT_LAYER, "digest": digest, "size": size})
        diff_ids.append(digest)
        history.append({"created": f"2026-02-0{i + 1}T00:00:00Z",
                        "created_by": lf.name})

    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "User": f"{run_uid}:{run_gid}",
            "WorkingDir": "/app",
            "Env": ["PATH=/opt/bin1:/opt/bin2", "DEMO=entrypoint"],
            "Entrypoint": ["serve"],
            "Cmd": ["--host", "0.0.0.0", "--port", "8080"],
        },
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": history,
    }
    config_digest, config_size = put_blob(
        blobs, json.dumps(config, sort_keys=True).encode())
    manifest = {
        "schemaVersion": 2,
        "mediaType": MT_MANIFEST,
        "config": {"mediaType": MT_CONFIG, "digest": config_digest,
                   "size": config_size},
        "layers": layer_descs,
    }
    manifest_digest, manifest_size = put_blob(
        blobs, json.dumps(manifest, sort_keys=True).encode())
    return {
        "mediaType": MT_MANIFEST,
        "digest": manifest_digest,
        "size": manifest_size,
        "annotations": {REF_NAME: tag},
        "platform": {"architecture": "amd64", "os": "linux"},
    }


def write_layer_to_blobs(blobs: Path, layer_path: Path) -> tuple[str, int]:
    data = layer_path.read_bytes()
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    dst = blobs / digest.split(":", 1)[1]
    if not dst.exists():
        dst.write_bytes(data)
    return digest, len(data)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("layout", type=Path, help="OCI layout directory to create")
    p.add_argument("--force", action="store_true")
    p.add_argument("--uid", type=int, default=1000, help="demo non-zero UID")
    p.add_argument("--gid", type=int, default=1000, help="demo single GID")
    args = p.parse_args()

    layout: Path = args.layout
    if layout.exists():
        if not args.force:
            print(f"error: {layout} exists (use --force)", file=sys.stderr)
            return 2
        shutil.rmtree(layout)
    blobs = layout / "blobs" / "sha256"
    build = layout / ".build-tmp"
    build.mkdir(parents=True)
    blobs.mkdir(parents=True)

    base_tar = build / "base.tar"
    chmod_tar = build / "chmod-bin1.tar"
    lock_tar = build / "lock-app.tar"
    with tarfile.open(base_tar, "w") as tf:
        base_layer(tf)
    with tarfile.open(chmod_tar, "w") as tf:
        chmod_bin1_layer(tf)
    with tarfile.open(lock_tar, "w") as tf:
        lock_workdir_layer(tf)

    try:
        manifests = [
            make_image(blobs, [base_tar], "entrydemo:1.0", args.uid, args.gid),
            make_image(blobs, [base_tar, chmod_tar], "entrydemo:1.1",
                       args.uid, args.gid),
            make_image(blobs, [base_tar, chmod_tar, lock_tar], "entrydemo:wd",
                       args.uid, args.gid),
        ]
        (layout / "oci-layout").write_text(
            json.dumps({"imageLayoutVersion": "1.0.0"}))
        (layout / "index.json").write_text(json.dumps(
            {"schemaVersion": 2, "manifests": manifests}, indent=2))
    finally:
        shutil.rmtree(build, ignore_errors=True)

    print(f"built entrypoint demo tags in {layout}:")
    for m in manifests:
        print(f"  {m['annotations'][REF_NAME]:16s} {m['digest']}")
    print()
    print("Try in /docs (POST /verify-entrypoint) or:")
    for tag, expect in (
        ("entrydemo:1.0", "/opt/bin1/serve"),
        ("entrydemo:1.1", "/opt/bin2/serve (bin1 blocked by upper-layer chmod)"),
        ("entrydemo:wd", "working dir /app blocked"),
    ):
        print(f"  curl -s localhost:8080/verify-entrypoint -H 'Content-Type: application/json' "
              f"-d '{{\"tag\":\"{tag}\",\"uid\":{args.uid},\"gid\":{args.gid}}}'")
        print(f"      -> expect {expect}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

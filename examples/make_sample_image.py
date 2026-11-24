#!/usr/bin/env python3
"""Build a 3-layer demo image (uncompressed tar layers) in an OCI layout.

The image is crafted so that the *top* layer contains an opaque whiteout on
``/app/data`` (masking everything base and patch1 put there) followed by new
files in that same directory — the scenario the merge demo showcases:

    base.tar    /app/data/old.log, /app/data/base.txt, /etc/keep.cfg, /etc/old.cfg
    patch1.tar  + /app/data/junk.txt, /app/data/tmpdir/scratch.bin,
                ~ /etc/old.cfg (modified), + /app/server.bin (v1)
    patch2.tar  OPAQUE /app/data  (masks old.log, base.txt, junk.txt, tmpdir/)
                + /app/data/fresh.txt          <- new file in the masked dir
                - /etc/old.cfg (whiteout), + /etc/new.cfg
                ~ /app/server.bin (v2)

Usage: make_sample_image.py [--force] [--tag TAG] LAYOUT_DIR
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


def _ti(name: str, mode: int, uid: int = 0, gid: int = 0, mtime: int = 1_700_000_000,
        type_: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.type = type_
    ti.mode, ti.uid, ti.gid, ti.mtime = mode, uid, gid, mtime
    ti.uname, ti.gname = (f"u{uid}" if uid else "root"), (f"g{gid}" if gid else "root")
    return ti


def _add_file(tf: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644,
              uid: int = 0, gid: int = 0, mtime: int = 1_700_000_000) -> None:
    ti = _ti(name, mode, uid, gid, mtime)
    ti.size = len(data)
    tf.addfile(ti, io.BytesIO(data))


def _add_dir(tf: tarfile.TarFile, name: str, mode: int = 0o755,
             uid: int = 0, gid: int = 0, mtime: int = 1_700_000_000) -> None:
    tf.addfile(_ti(name, mode, uid, gid, mtime, tarfile.DIRTYPE))


def build_layers(work: Path) -> list[Path]:
    work.mkdir(parents=True, exist_ok=True)

    base = work / "base.tar"
    with tarfile.open(base, "w", format=tarfile.PAX_FORMAT) as tf:
        _add_dir(tf, "app")
        _add_dir(tf, "app/data")
        _add_file(tf, "app/data/old.log", b"old log from base\n")
        _add_file(tf, "app/data/base.txt", b"base file\n")
        _add_dir(tf, "etc")
        _add_file(tf, "etc/keep.cfg", b"keep=1\n")
        _add_file(tf, "etc/old.cfg", b"old=1\n")

    patch1 = work / "patch1.tar"
    with tarfile.open(patch1, "w", format=tarfile.PAX_FORMAT) as tf:
        _add_file(tf, "app/data/junk.txt", b"junk added by patch1\n")
        _add_dir(tf, "app/data/tmpdir")
        _add_file(tf, "app/data/tmpdir/scratch.bin", b"scratch\n")
        _add_file(tf, "app/server.bin", b"server-binary-v1\n", mode=0o755)
        _add_file(tf, "etc/old.cfg", b"old=2  # modified by patch1\n")

    patch2 = work / "patch2.tar"
    with tarfile.open(patch2, "w", format=tarfile.PAX_FORMAT) as tf:
        # mask everything lower layers put into /app/data ...
        _add_file(tf, "app/data/.wh..wh..opq", b"")
        # ... then add fresh files to the same directory
        _add_file(tf, "app/data/fresh.txt", b"fresh file added after masking\n")
        _add_file(tf, "etc/.wh.old.cfg", b"")  # delete the modified config
        _add_file(tf, "etc/new.cfg", b"new=1\n")
        _add_file(tf, "app/server.bin", b"server-binary-v2\n", mode=0o755)

    return [base, patch1, patch2]


def write_blob(blobs: Path, data: bytes) -> tuple[str, int]:
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    (blobs / digest.split(":", 1)[1]).write_bytes(data)
    return digest, len(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layout", type=Path, help="OCI layout directory to create")
    parser.add_argument("--tag", default="demo:1.0.1", help="tag for the image")
    parser.add_argument("--force", action="store_true", help="rebuild even if the dir exists")
    args = parser.parse_args()

    layout: Path = args.layout
    if layout.exists():
        if not args.force:
            print(f"error: {layout} already exists (use --force to rebuild)", file=sys.stderr)
            return 2
        shutil.rmtree(layout)
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)

    work = layout / ".build-tmp"
    try:
        layer_paths = build_layers(work)
        layer_descs, diff_ids, history = [], [], [
            {"created": "2026-01-01T00:00:00Z", "created_by": "set base environment",
             "empty_layer": True},
        ]
        for i, path in enumerate(layer_paths):
            digest, size = write_blob(blobs, path.read_bytes())
            layer_descs.append({"mediaType": MT_LAYER, "digest": digest, "size": size})
            diff_ids.append(digest)  # uncompressed layer: diff_id == blob digest
            history.append({"created": f"2026-01-0{i + 2}T00:00:00Z",
                            "created_by": f"add layer {path.name}"})

        config = {
            "architecture": "amd64",
            "os": "linux",
            "created": "2026-01-04T00:00:00Z",
            "config": {
                "User": "1000",
                "WorkingDir": "/app",
                "Env": ["MODE=demo", "PORT=8080"],
                "Cmd": ["/app/server.bin", "--serve"],
                "ExposedPorts": {"8080/tcp": {}},
                "Labels": {"app": "demo", "stage": "three-layers"},
            },
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
            "history": history,
        }
        config_digest, config_size = write_blob(blobs, json.dumps(config).encode())

        manifest = {
            "schemaVersion": 2,
            "mediaType": MT_MANIFEST,
            "config": {"mediaType": MT_CONFIG, "digest": config_digest, "size": config_size},
            "layers": layer_descs,
        }
        manifest_digest, manifest_size = write_blob(blobs, json.dumps(manifest).encode())

        (layout / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "1.0.0"}))
        (layout / "index.json").write_text(json.dumps({
            "schemaVersion": 2,
            "manifests": [{
                "mediaType": MT_MANIFEST,
                "digest": manifest_digest,
                "size": manifest_size,
                "annotations": {REF_NAME: args.tag},
                "platform": {"architecture": "amd64", "os": "linux"},
            }],
        }, indent=2))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"built {args.tag} in {layout} (3 layers, uncompressed)")
    print(f"  manifest: {manifest_digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

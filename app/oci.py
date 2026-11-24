"""Primitives for reading and updating a local OCI image layout.

Layout recap::

    <layout>/
      oci-layout            {"imageLayoutVersion": "1.0.0"}
      index.json            {"schemaVersion": 2, "manifests": [descriptor, ...]}
      blobs/sha256/<hex>    content-addressed blobs (manifests, configs, layers)

Tags live in the ``org.opencontainers.image.ref.name`` annotation of the
manifest descriptors in ``index.json``.

All writes follow the same publish pattern: write to a temporary file in the
same directory, fsync, then atomically ``os.replace`` into place.  The
``index.json`` is always updated last, so a crashed or failed merge never
publishes a half-written tag and never touches the source tag.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

OCI_LAYOUT_VERSION = "1.0.0"
REF_NAME_ANNOTATION = "org.opencontainers.image.ref.name"

MT_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MT_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MT_DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
MT_DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"

MANIFEST_MEDIA_TYPES = {MT_OCI_MANIFEST, MT_DOCKER_MANIFEST}
INDEX_MEDIA_TYPES = {MT_OCI_INDEX, MT_DOCKER_LIST}

# Uncompressed tar layer media types (the only ones we can merge).
MT_OCI_LAYER_TAR = "application/vnd.oci.image.layer.v1.tar"
MT_DOCKER_LAYER_TAR = "application/vnd.docker.image.rootfs.diff.tar"
UNCOMPRESSED_LAYER_TYPES = {MT_OCI_LAYER_TAR, MT_DOCKER_LAYER_TAR}


class LayoutError(Exception):
    """Base class for layout related failures."""


class TagNotFound(LayoutError):
    pass


class TagExists(LayoutError):
    pass


class UnsupportedImage(LayoutError):
    """The image cannot be handled by this service (multi-platform, compressed
    layers, malformed metadata, ...)."""


def _parse_digest(digest: str) -> tuple[str, str]:
    algo, sep, hexd = digest.partition(":")
    if not sep or algo != "sha256" or not hexd:
        raise LayoutError(f"unsupported digest: {digest!r}")
    return algo, hexd


class OciLayout:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # -- paths --------------------------------------------------------------

    @property
    def blobs_dir(self) -> Path:
        return self.root / "blobs" / "sha256"

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def blob_path(self, digest: str) -> Path:
        algo, hexd = _parse_digest(digest)
        return self.root / "blobs" / algo / hexd

    # -- low level IO ---------------------------------------------------------

    def _atomic_write(self, final: Path, write_fn: Callable[[BinaryIO], None]) -> None:
        """Write via temp file + fsync + atomic rename."""
        final.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=final.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                write_fn(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)
            # fsync the directory so the rename itself is durable
            dirfd = os.open(final.parent, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def write_blob_bytes(self, data: bytes) -> tuple[str, int]:
        """Content-addressed write of a byte blob. Returns (digest, size)."""
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        final = self.blob_path(digest)
        if final.exists():
            return digest, len(data)
        self._atomic_write(final, lambda f: f.write(data))
        return digest, len(data)

    def write_blob_stream(self, write_fn: Callable[[BinaryIO], None]) -> tuple[str, int]:
        """Stream a blob to a temp file while hashing, then publish it under
        its content digest. Returns (digest, size)."""
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.blobs_dir, prefix=".tmp-")
        hasher = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as raw:
                class _HashingWriter:
                    def write(self, b: bytes) -> int:
                        nonlocal size
                        hasher.update(b)
                        size += len(b)
                        return raw.write(b)

                    def tell(self) -> int:
                        return raw.tell()

                    def flush(self) -> None:
                        raw.flush()

                write_fn(_HashingWriter())  # type: ignore[arg-type]
                raw.flush()
                os.fsync(raw.fileno())
            digest = "sha256:" + hasher.hexdigest()
            final = self.blob_path(digest)
            if final.exists():
                os.unlink(tmp)  # identical content already stored
            else:
                os.replace(tmp, final)
                dirfd = os.open(final.parent, os.O_RDONLY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
            return digest, size
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def read_blob_bytes(self, digest: str) -> bytes:
        path = self.blob_path(digest)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise LayoutError(f"blob missing from layout: {digest}") from None

    def read_blob_json(self, digest: str) -> Any:
        return json.loads(self.read_blob_bytes(digest))

    def open_blob(self, digest: str) -> BinaryIO:
        path = self.blob_path(digest)
        try:
            return open(path, "rb")
        except FileNotFoundError:
            raise LayoutError(f"blob missing from layout: {digest}") from None

    # -- index / tags -----------------------------------------------------------

    def load_index(self) -> dict:
        try:
            return json.loads(self.index_path.read_text())
        except FileNotFoundError:
            raise LayoutError(f"not an OCI layout (no index.json): {self.root}") from None

    def _write_index(self, index: dict) -> None:
        payload = json.dumps(index, indent=2, sort_keys=False).encode()
        self._atomic_write(self.index_path, lambda f: f.write(payload))

    @staticmethod
    def find_tag(index: dict, tag: str) -> dict | None:
        for desc in index.get("manifests", []):
            if desc.get("annotations", {}).get(REF_NAME_ANNOTATION) == tag:
                return desc
        return None

    def publish_manifest(self, manifest_desc: dict) -> None:
        """Append a manifest descriptor to index.json (the tag goes live)."""
        index = self.load_index()
        tag = manifest_desc.get("annotations", {}).get(REF_NAME_ANNOTATION)
        if tag and self.find_tag(index, tag):
            raise TagExists(f"tag already exists: {tag}")
        index.setdefault("manifests", []).append(manifest_desc)
        self._write_index(index)

    def list_tags(self) -> list[str]:
        index = self.load_index()
        return sorted(
            d["annotations"][REF_NAME_ANNOTATION]
            for d in index.get("manifests", [])
            if REF_NAME_ANNOTATION in d.get("annotations", {})
        )


@dataclass
class ImageRef:
    """A resolved single-platform image."""

    descriptor: dict
    manifest: dict
    config: dict


def load_image(layout: OciLayout, tag: str) -> ImageRef:
    """Resolve a tag to (descriptor, manifest, config), enforcing the
    single-platform constraint."""
    index = layout.load_index()
    desc = OciLayout.find_tag(index, tag)
    if desc is None:
        raise TagNotFound(f"tag not found: {tag}")
    media_type = desc.get("mediaType", "")
    if media_type in INDEX_MEDIA_TYPES:
        raise UnsupportedImage(
            f"{tag}: multi-platform image indexes are not supported, "
            "only single-platform image manifests"
        )
    if media_type and media_type not in MANIFEST_MEDIA_TYPES:
        raise UnsupportedImage(f"{tag}: unsupported manifest media type {media_type!r}")
    manifest = layout.read_blob_json(desc["digest"])
    if "manifests" in manifest:  # defensive: a manifest list behind our back
        raise UnsupportedImage(f"{tag}: manifest list is not supported")
    config_desc = manifest.get("config") or {}
    config = layout.read_blob_json(config_desc.get("digest", ""))
    return ImageRef(descriptor=desc, manifest=manifest, config=config)

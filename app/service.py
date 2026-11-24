"""Orchestrates a merge: resolve tags, merge the top two layers, rewrite the
image config/manifest and publish the new tag atomically."""

from __future__ import annotations

import copy
import json
import tarfile
import threading
from datetime import datetime, timezone
from typing import BinaryIO

from . import oci
from .merge import LayerMerger

# Serialize merges: index.json is read-modify-written and blob publishing is
# not coordinated with anyone else.  One process-wide lock is enough for the
# single-worker deployment this service targets.
_merge_lock = threading.Lock()


class MergeResult(dict):
    """Plain dict subclass used for the API response payload."""


def _sniff_compressed(f: BinaryIO, digest: str) -> None:
    """Reject gzip/zstd content even if the descriptor claims plain tar."""
    head = f.read(4)
    f.seek(0)
    if head[:2] == b"\x1f\x8b":
        raise oci.UnsupportedImage(f"layer {digest} is gzip compressed, cannot merge")
    if head == b"\x28\xb5\x2f\xfd":
        raise oci.UnsupportedImage(f"layer {digest} is zstd compressed, cannot merge")


def _open_layer_tar(
    layout: oci.OciLayout, desc: dict
) -> tuple[BinaryIO, tarfile.TarFile | None]:
    """Open a layer blob as a random-access tar.

    Returns ``(raw_file, tarfile_or_None)``; the tarfile is ``None`` for a
    zero-size blob, which is a valid empty layer.
    """
    media_type = desc.get("mediaType", "")
    if media_type not in oci.UNCOMPRESSED_LAYER_TYPES:
        raise oci.UnsupportedImage(
            f"layer {desc.get('digest')} has media type {media_type!r}; "
            "only uncompressed tar layers can be merged"
        )
    raw = layout.open_blob(desc["digest"])
    try:
        _sniff_compressed(raw, desc["digest"])
        if desc.get("size") == 0:
            return raw, None
        try:
            tf = tarfile.open(fileobj=raw, mode="r:")
        except tarfile.ReadError as exc:
            raise oci.UnsupportedImage(
                f"layer {desc['digest']} is not a readable uncompressed tar: {exc}"
            ) from None
        return raw, tf
    except BaseException:
        raw.close()
        raise


def _rewrite_config(config: dict, new_diff_id: str) -> dict:
    """Return a copy of the image config with the last two diff_ids replaced
    by the merged layer's diff_id and a matching history entry.  Everything
    else — including the startup config (Cmd/Entrypoint/Env/...) — is kept
    byte-for-byte identical."""
    cfg = copy.deepcopy(config)
    rootfs = cfg.get("rootfs") or {}
    diff_ids = rootfs.get("diff_ids") or []
    if len(diff_ids) < 2:
        raise oci.UnsupportedImage("image config has fewer than two diff_ids")
    rootfs["diff_ids"] = diff_ids[:-2] + [new_diff_id]
    cfg["rootfs"] = rootfs

    history = cfg.get("history")
    if isinstance(history, list) and history:
        non_empty = [i for i, h in enumerate(history) if not h.get("empty_layer")]
        merged_entry = {
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "created_by": "layer-merger: squashed top 2 layers",
        }
        if len(non_empty) >= 2:
            i, j = non_empty[-2], non_empty[-1]
            # collapse entries i..j into one, keeping empty_layer entries and
            # inheriting the newest entry's extra fields (author, comment...)
            kept = [h for h in history[i : j + 1] if h.get("empty_layer")]
            merged_entry = {**history[j], **merged_entry}
            cfg["history"] = history[:i] + kept + [merged_entry] + history[j + 1 :]
        else:
            cfg["history"] = history + [merged_entry]
    return cfg


def merge_top_two_layers(layout_dir: str, source_tag: str, target_tag: str) -> MergeResult:
    """Merge the last two layers of ``source_tag`` and publish as ``target_tag``.

    On any failure the source tag is untouched and the target tag is not
    published (index.json is only rewritten after every blob is in place).
    """
    if source_tag == target_tag:
        raise ValueError("source and target tags must differ")

    layout = oci.OciLayout(layout_dir)
    with _merge_lock:
        index = layout.load_index()
        if oci.OciLayout.find_tag(index, target_tag) is not None:
            raise oci.TagExists(f"tag already exists: {target_tag}")

        image = oci.load_image(layout, source_tag)
        manifest = image.manifest
        config = image.config

        layers = manifest.get("layers") or []
        if len(layers) < 2:
            raise oci.UnsupportedImage(
                f"{source_tag}: need at least 2 layers to merge, found {len(layers)}"
            )
        diff_ids = (config.get("rootfs") or {}).get("diff_ids") or []
        if len(diff_ids) != len(layers):
            raise oci.UnsupportedImage(
                f"{source_tag}: config diff_ids ({len(diff_ids)}) do not match "
                f"manifest layers ({len(layers)})"
            )

        lower_desc, upper_desc = layers[-2], layers[-1]

        # -- merge the two layer tars into one streamed blob ---------------
        # With no base layers the two merged layers are the whole image, so
        # implicit directories they leave behind must be materialized.
        merger = LayerMerger(synthesize_implicit_dirs=len(layers) == 2)
        raws: list[BinaryIO] = []
        sources: list[tarfile.TarFile | None] = []
        try:
            for source_idx, desc in enumerate((lower_desc, upper_desc)):
                raw, tf = _open_layer_tar(layout, desc)
                raws.append(raw)
                sources.append(tf)
                if tf is not None:
                    merger.add_layer(tf, source_idx)

            layer_digest, layer_size = layout.write_blob_stream(
                lambda w: merger.write_merged(sources, w)  # streams via small buffer
            )
        finally:
            for tf in sources:
                if tf is not None:
                    try:
                        tf.close()
                    except Exception:
                        pass
            for raw in raws:
                try:
                    raw.close()
                except Exception:
                    pass

        # -- rewrite config (diff_ids + history; startup config preserved) --
        new_config = _rewrite_config(config, new_diff_id=layer_digest)
        config_bytes = json.dumps(new_config, separators=(",", ":"), sort_keys=True).encode()
        config_digest, config_size = layout.write_blob_bytes(config_bytes)

        # -- rewrite manifest ------------------------------------------------
        new_manifest = copy.deepcopy(manifest)
        merged_layer_desc = {
            "mediaType": upper_desc.get("mediaType", oci.MT_OCI_LAYER_TAR),
            "digest": layer_digest,
            "size": layer_size,
        }
        new_manifest["layers"] = layers[:-2] + [merged_layer_desc]
        new_config_desc = dict(manifest.get("config") or {})
        new_config_desc["digest"] = config_digest
        new_config_desc["size"] = config_size
        new_manifest["config"] = new_config_desc
        manifest_bytes = json.dumps(new_manifest, separators=(",", ":"), sort_keys=True).encode()
        manifest_digest, manifest_size = layout.write_blob_bytes(manifest_bytes)

        # -- publish the tag last (atomic index.json update) ----------------
        new_desc = {
            "mediaType": image.descriptor.get("mediaType", oci.MT_OCI_MANIFEST),
            "digest": manifest_digest,
            "size": manifest_size,
            "annotations": {
                **(image.descriptor.get("annotations") or {}),
                oci.REF_NAME_ANNOTATION: target_tag,
            },
        }
        if "platform" in image.descriptor:
            new_desc["platform"] = image.descriptor["platform"]
        layout.publish_manifest(new_desc)

        return MergeResult(
            source_tag=source_tag,
            target_tag=target_tag,
            manifest_digest=manifest_digest,
            config_digest=config_digest,
            merged_layer={
                "digest": layer_digest,
                "diff_id": layer_digest,  # uncompressed layer: diff_id == blob digest
                "size": layer_size,
                "mediaType": merged_layer_desc["mediaType"],
            },
            layers=[d["digest"] for d in new_manifest["layers"]],
            base_layers_kept=len(layers) - 2,
            layers_merged=2,
        )

"""Merge the top two layers of an OCI image into a single uncompressed tar layer.

Semantics implemented here follow the OCI image layer spec
(https://github.com/opencontainers/image-spec/blob/main/layer.md):

* regular files and directories are applied in order;
* a plain whiteout ``.wh.<name>`` deletes ``<name>`` (file or dir subtree)
  from the view of lower layers;
* an opaque whiteout ``.wh..wh..opq`` inside directory ``d`` hides everything
  that lower layers contributed under ``d``, while entries that the *same*
  layer adds under ``d`` remain visible.

Only regular files, directories, plain whiteouts and opaque whiteouts are
supported; anything else (symlinks, hardlinks, devices, ...) raises
:class:`MergeError` so the caller can refuse the merge instead of silently
producing a wrong layer.

Resource contract
-----------------
* File contents are never loaded into memory: phase 1 reads tar headers only,
  phase 2 streams file data from the source blobs into the new layer through
  a small fixed buffer.  Memory usage is O(number of entries), independent of
  file sizes.
* Nothing but the new layer blob is written to disk.
"""

from __future__ import annotations

import tarfile
from dataclasses import dataclass

WHITEOUT_PREFIX = ".wh."
OPAQUE_WHITEOUT = ".wh..wh..opq"


class MergeError(Exception):
    """Raised when a layer cannot be merged (unsupported or invalid input)."""


@dataclass
class _Item:
    """A surviving tar entry plus the layer it came from."""

    tarinfo: tarfile.TarInfo
    source: int  # index into the list of source TarFiles (0 = lower, 1 = upper)


def _normalize(name: str) -> str | None:
    """Normalize a tar entry name to a clean relative path.

    Returns ``None`` for the root entry itself.  Raises :class:`MergeError`
    for absolute paths or paths that escape the root.
    """
    # Tar names use '/' separators; collapse redundant parts.
    parts: list[str] = []
    for comp in name.split("/"):
        if comp in ("", "."):
            continue
        if comp == "..":
            raise MergeError(f"tar entry escapes root: {name!r}")
        parts.append(comp)
    if not parts:
        return None
    return "/".join(parts)


def _classify(ti: tarfile.TarInfo) -> tuple[str, str]:
    """Classify a tar entry.

    Returns ``(kind, key)`` where kind is one of ``file``, ``dir``,
    ``whiteout``, ``opaque`` and key is the normalized path the entry
    applies to (for whiteouts: the *target* path; for opaque markers: the
    directory that becomes opaque; otherwise the entry's own path).
    """
    norm = _normalize(ti.name)
    if norm is None:
        return "root", ""

    parent, _, base = norm.rpartition("/")
    if base == OPAQUE_WHITEOUT:
        return "opaque", parent
    if base.startswith(WHITEOUT_PREFIX):
        target_base = base[len(WHITEOUT_PREFIX):]
        if not target_base:
            raise MergeError(f"invalid whiteout entry: {ti.name!r}")
        target = f"{parent}/{target_base}" if parent else target_base
        return "whiteout", target

    if ti.isdir():
        return "dir", norm
    if ti.isreg():
        return "file", norm
    raise MergeError(
        f"unsupported tar entry type {ti.type!r} for {ti.name!r}: "
        "only regular files, directories and whiteouts are supported"
    )


class LayerMerger:
    """Accumulates two layers and writes out their merged equivalent.

    ``synthesize_implicit_dirs``: when the two merged layers are the *whole*
    image (no base layers), directories that were only created implicitly by
    tar extraction (a file needed its parent) and whose contents were all
    deleted/masked again would silently vanish from the merged result.  With
    no base layers we have complete information, so explicit directory
    entries (0755 root:root, matching applier defaults) are synthesized for
    those.  With base layers present this is never done: the base may hold
    the directory with its own metadata, which must not be clobbered.
    """

    def __init__(self, synthesize_implicit_dirs: bool = False) -> None:
        # real entries and plain whiteouts, keyed by normalized target path
        self._entries: dict[str, _Item] = {}
        # opaque markers, keyed by normalized directory path
        self._opaque: dict[str, _Item] = {}
        # directories that exist only implicitly (no explicit dir entry),
        # mapped to the layer index that first implied them
        self._implicit: dict[str, int] = {}
        self._synthesize = synthesize_implicit_dirs

    # -- state mutation helpers -------------------------------------------

    def _drop_subtree(self, path: str) -> None:
        """Forget everything at ``path`` and below (all kinds of entries)."""
        self._entries.pop(path, None)
        self._opaque.pop(path, None)
        self._implicit.pop(path, None)
        prefix = path + "/"
        for store in (self._entries, self._opaque, self._implicit):
            for key in [k for k in store if k.startswith(prefix)]:
                del store[key]

    def _put_real(self, path: str, item: _Item, is_dir: bool) -> None:
        if not is_dir:
            # A file replaces anything that used to live at/below this path
            # (e.g. a directory subtree from a lower layer).
            self._drop_subtree(path)
        else:
            # An explicit dir entry: no longer merely implicit.
            self._implicit.pop(path, None)
        self._entries[path] = item

    def _put_whiteout(self, target: str, item: _Item) -> None:
        # The whiteout deletes the target subtree contributed by lower
        # layers.  We keep the whiteout itself (instead of just dropping the
        # lower entries) because the base layers below the merged layer may
        # still contain the target: the deletion must keep applying to them.
        self._drop_subtree(target)
        self._entries[target] = item

    def _put_opaque(self, directory: str, item: _Item, source: int) -> None:
        # The opaque marker hides entries strictly below ``directory`` that
        # came from *lower* layers.  Entries from the same layer (no matter
        # whether they appear before or after the marker in the tar stream)
        # stay visible.
        prefix = directory + "/" if directory else ""
        for store in (self._entries, self._opaque, self._implicit):
            for key in [k for k in store if k != directory and k.startswith(prefix)]:
                entry_source = store[key] if store is self._implicit else store[key].source
                if entry_source < source:
                    del store[key]
        self._opaque[directory] = item

    def _note_implicit_parents(self, path: str, source: int) -> None:
        """Record ancestors of a real entry that have no explicit dir entry."""
        comps = path.split("/")
        for i in range(1, len(comps)):
            ancestor = "/".join(comps[:i])
            if ancestor not in self._entries and ancestor not in self._implicit:
                self._implicit[ancestor] = source

    # -- public API --------------------------------------------------------

    def add_layer(self, tf: tarfile.TarFile, source: int) -> None:
        """Scan a layer tar (headers only) and fold it into the merge state."""
        for ti in tf:
            kind, key = _classify(ti)
            if kind == "root":
                continue
            # Normalize the stored name so the merged layer is clean and
            # deterministic; scrub stale pax overrides of the old name.
            if kind in ("file", "dir"):
                ti.name = key
            elif kind == "whiteout":
                parent, _, base = key.rpartition("/")
                ti.name = f"{parent}/{WHITEOUT_PREFIX}{base}" if parent else WHITEOUT_PREFIX + base
            else:  # opaque
                ti.name = f"{key}/{OPAQUE_WHITEOUT}" if key else OPAQUE_WHITEOUT
            ti.pax_headers.pop("path", None)
            ti.pax_headers.pop("linkpath", None)

            item = _Item(tarinfo=ti, source=source)
            if kind == "file":
                self._put_real(key, item, is_dir=False)
                self._note_implicit_parents(key, source)
            elif kind == "dir":
                self._put_real(key, item, is_dir=True)
                self._note_implicit_parents(key, source)
            elif kind == "whiteout":
                self._put_whiteout(key, item)
            else:
                self._put_opaque(key, item, source)

    def _synthesized_dir_items(self) -> list[_Item]:
        """Explicit dir entries for implicit dirs that nothing else creates.

        A surviving real entry or opaque marker re-creates its ancestors
        during extraction; a whiteout does not.  Any implicit dir not covered
        by those would be lost, so materialize it with applier defaults.
        """
        creators: list[str] = []
        for item in self._entries.values():
            base = item.tarinfo.name.rsplit("/", 1)[-1]
            if base.startswith(WHITEOUT_PREFIX):
                continue  # whiteouts create nothing
            creators.append(item.tarinfo.name)
        creators.extend(self._opaque.keys())  # opaque dir materializes itself

        synthesized: list[_Item] = []
        for imp in sorted(self._implicit):
            prefix = imp + "/"
            if any(c == imp or c.startswith(prefix) for c in creators):
                continue
            ti = tarfile.TarInfo(imp)
            ti.type = tarfile.DIRTYPE
            ti.mode, ti.uid, ti.gid, ti.mtime = 0o755, 0, 0, 0
            ti.uname, ti.gname = "root", "root"
            synthesized.append(_Item(tarinfo=ti, source=-1))
        return synthesized

    def write_merged(self, sources: list[tarfile.TarFile | None], fileobj) -> int:
        """Stream the merged layer as an uncompressed tar into ``fileobj``.

        ``sources`` are the still-open source tars (index matches the
        ``source`` argument of :meth:`add_layer`); an entry is ``None`` for
        an empty source layer, which no surviving item can reference.
        Returns the number of entries written.
        """
        items = list(self._entries.values()) + list(self._opaque.values())
        if self._synthesize:
            items += self._synthesized_dir_items()
        # Parents sort before their children; whiteouts (".wh.*") sort before
        # the subtree they delete, which is required for correct application.
        items.sort(key=lambda it: it.tarinfo.name.split("/"))

        count = 0
        with tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT) as out:
            for item in items:
                ti = item.tarinfo
                if ti.isreg():
                    src_tf = sources[item.source]
                    if src_tf is None:  # pragma: no cover - defensive
                        raise MergeError(f"entry {ti.name!r} refers to an empty layer")
                    src = src_tf.extractfile(ti)
                    if src is None:  # pragma: no cover - defensive
                        raise MergeError(f"cannot read data for {ti.name!r}")
                    with src:
                        out.addfile(ti, src)
                else:
                    out.addfile(ti)
                count += 1
        return count

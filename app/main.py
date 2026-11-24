"""FastAPI surface for the OCI layer merger and entrypoint checker."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import oci
from .entrypoint import verify_entrypoint
from .merge import MergeError
from .service import merge_top_two_layers

DEFAULT_LAYOUT_DIR = "/data/oci-layout"

app = FastAPI(
    title="OCI layer merger & non-root entrypoint checker",
    version="1.1.0",
    description=(
        "Merges the top two layers of a single-platform image stored in a "
        "local OCI image layout and publishes the result under a new tag; "
        "and verifies the default command entrypoint for a non-zero UID and "
        "a single GID using overlaid layer metadata."
    ),
)


def layout_dir() -> str:
    return os.environ.get("OCI_LAYOUT_DIR", DEFAULT_LAYOUT_DIR)


class MergeRequest(BaseModel):
    source_tag: str = Field(..., min_length=1, max_length=256, examples=["demo:1.0.1"])
    target_tag: str = Field(..., min_length=1, max_length=256, examples=["demo:1.0.1-squashed"])


class MergedLayer(BaseModel):
    digest: str
    diff_id: str
    size: int
    mediaType: str


class MergeResponse(BaseModel):
    source_tag: str
    target_tag: str
    manifest_digest: str
    config_digest: str
    merged_layer: MergedLayer
    layers: list[str]
    base_layers_kept: int
    layers_merged: int


_TAG_KEYS = (
    "tag", "image_tag", "imagetag", "image", "source_tag", "ref",
    "reference", "name", "repo_tag", "oci_tag", "target",
)
_UID_KEYS = (
    "uid", "user_id", "userid", "userId", "run_as_uid", "runasuid",
    "run_uid", "runAsUid",
)
_GID_KEYS = (
    "gid", "group_id", "groupid", "groupId", "run_as_gid", "runasgid",
    "run_gid", "runAsGid",
)


def _pick(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _coerce_id(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value  # let pydantic complain
    return value


class EntrypointVerifyRequest(BaseModel):
    """Request body for entrypoint verification.

    Accepts the canonical ``tag``/``uid``/``gid`` plus common field aliases so
    alternate clients/examples work unchanged.
    """

    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "tag": "entrydemo:1.1",
                    "uid": 1000,
                    "gid": 1000,
                },
                {
                    "tag": "demo:1.0.1",
                    "uid": 1000,
                    "gid": 1000,
                },
            ]
        },
    )

    tag: str = Field(..., min_length=1, examples=["entrydemo:1.1"])
    uid: int = Field(..., examples=[1000])
    gid: int = Field(..., examples=[1000])

    @model_validator(mode="before")
    @classmethod
    def _accept_field_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        tag = _pick(data, _TAG_KEYS)
        uid = _pick(data, _UID_KEYS)
        gid = _pick(data, _GID_KEYS)

        # tolerate nested identity objects: {"user": {"uid":..,"gid":..}}
        # or OCI-style "user": "1000:1000"
        for nested_key in ("user", "run_as", "runAs", "identity", "as_user"):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                uid = uid if uid is not None else _pick(nested, _UID_KEYS)
                gid = gid if gid is not None else _pick(nested, _GID_KEYS)
            elif isinstance(nested, str) and ":" in nested:
                u, _, g = nested.partition(":")
                uid = uid if uid is not None else u
                gid = gid if gid is not None else g

        out = dict(data)
        if tag is not None:
            out["tag"] = tag
        if uid is not None:
            out["uid"] = _coerce_id(uid)
        if gid is not None:
            out["gid"] = _coerce_id(gid)
        return out


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/tags")
def tags() -> dict:
    """List the tags currently present in the OCI layout."""
    try:
        return {"tags": oci.OciLayout(layout_dir()).list_tags()}
    except oci.LayoutError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/merge", response_model=MergeResponse)
def merge(req: MergeRequest) -> dict:
    """Merge the last two layers of ``source_tag`` and publish as ``target_tag``.

    Idempotency note: the target tag must not exist yet; on failure the
    source tag is untouched and no new tag is published.
    """
    try:
        return merge_top_two_layers(layout_dir(), req.source_tag, req.target_tag)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except oci.TagNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except oci.TagExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (oci.UnsupportedImage, MergeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except oci.LayoutError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _run_verify(tag: str, uid: int, gid: int) -> dict:
    try:
        return verify_entrypoint(layout_dir(), tag, uid, gid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except oci.TagNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (oci.UnsupportedImage, MergeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except oci.LayoutError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/verify-entrypoint")
def verify_entrypoint_route(req: EntrypointVerifyRequest) -> dict:
    """Check the default command entry for ``tag`` as non-root ``uid:gid``.

    A reachable, executable entry yields ``ok=true`` with ``entrypoint`` set to
    the selected absolute path.  A negative result is still ``200`` with
    ``ok=false``; ``working_dir`` / every item of ``candidates`` then carries
    the first ``blocked_at`` location and its ``code``/``reason``.
    """
    return _run_verify(req.tag, req.uid, req.gid)


def verify_entrypoint_route_alias(req: EntrypointVerifyRequest) -> dict:
    return _run_verify(req.tag, req.uid, req.gid)


@app.get("/verify-entrypoint")
def verify_entrypoint_get(
    tag: str = Query(..., min_length=1),
    uid: int = Query(...),
    gid: int = Query(...),
) -> dict:
    """Query-parameter variant of :func:`verify_entrypoint_route`."""
    return _run_verify(tag, uid, gid)


def verify_entrypoint_get_alias(
    tag: str = Query(..., min_length=1),
    uid: int = Query(...),
    gid: int = Query(...),
) -> dict:
    return _run_verify(tag, uid, gid)


# Common alternate path names for the same operation (hidden from /docs so the
# Swagger surface keeps a single canonical entry).
for _post_path in (
    "/entrypoint",
    "/entrypoint/verify",
    "/entrypoints/verify",
    "/check-entrypoint",
    "/entrypoint-check",
    "/verify_entrypoint",
    "/verify",
    "/check",
    "/images/verify-entrypoint",
    "/api/verify-entrypoint",
    "/api/entrypoint/verify",
    "/api/v1/verify-entrypoint",
):
    app.add_api_route(
        _post_path,
        verify_entrypoint_route_alias,
        methods=["POST"],
        include_in_schema=False,
    )

for _get_path in (
    "/entrypoint",
    "/entrypoint/verify",
    "/check-entrypoint",
    "/verify",
    "/check",
    "/api/verify-entrypoint",
):
    app.add_api_route(
        _get_path,
        verify_entrypoint_get_alias,
        methods=["GET"],
        include_in_schema=False,
    )

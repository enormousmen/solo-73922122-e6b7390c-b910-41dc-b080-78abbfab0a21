"""FastAPI surface for the OCI top-two-layer merger."""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import oci
from .merge import MergeError
from .service import merge_top_two_layers

DEFAULT_LAYOUT_DIR = "/data/oci-layout"

app = FastAPI(
    title="OCI layer merger",
    version="1.0.0",
    description=(
        "Merges the top two layers of a single-platform image stored in a "
        "local OCI image layout and publishes the result under a new tag."
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

"""Project serialization: layer tree, settings and base64 PNG rasters.

The document format is plain JSON.  Rasters and masks are embedded as
base64-encoded RGBA PNG bytes (the stdlib PNG writer in :mod:`raster`), so a
saved project is fully self-contained and loadable without external images.
"""

import base64
import io
import json

from .model import Group, Layer, Project
from .raster import Raster, read_png, write_png


def _raster_to_b64(raster):
    buf = io.BytesIO()
    write_png(buf, raster)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _b64_to_raster(text):
    raw = base64.b64decode(text.encode("ascii"))
    return read_png(io.BytesIO(raw))


def _node_to_dict(node):
    data = {
        "name": node.name,
        "kind": node.kind,
        "opacity": node.opacity,
        "blend_mode": node.blend_mode,
        "clipped": node.clipped,
        "mask_id": node.mask_id,
    }
    if isinstance(node, Layer):
        data["raster"] = _raster_to_b64(node.raster)
    else:
        data["isolated"] = node.isolated
        data["children"] = [_node_to_dict(c) for c in node.children]
    return data


def _node_from_dict(data):
    common = dict(
        name=data["name"],
        opacity=data.get("opacity", 1.0),
        blend_mode=data.get("blend_mode", "normal"),
        clipped=data.get("clipped", False),
        mask_id=data.get("mask_id"),
    )
    if data["kind"] == "layer":
        return Layer(raster=_b64_to_raster(data["raster"]), **common)
    return Group(
        children=[_node_from_dict(c) for c in data.get("children", [])],
        isolated=data.get("isolated", True),
        **common,
    )


def to_dict(project):
    return {
        "version": 1,
        "width": project.width,
        "height": project.height,
        "root": _node_to_dict(project.root),
        "masks": {mid: _raster_to_b64(r)
                  for mid, r in project.masks.items()},
    }


def from_dict(data):
    root = _node_from_dict(data["root"])
    project = Project(data["width"], data["height"], root)
    for mid, encoded in data.get("masks", {}).items():
        project.add_mask(mid, _b64_to_raster(encoded))
    return project


def save(project, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_dict(project), f)


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return from_dict(json.load(f))

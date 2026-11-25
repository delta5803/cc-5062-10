"""Local painting window server (zero-dependency stdlib HTTP).

The browser is only a view and input device.  Every composited pixel returned
to it is produced by :class:`~layerpaint.engine.Evaluator`; the server never
invents blend math in JavaScript.  Endpoints:

    GET  /                 painting window (static HTML/JS)
    GET  /api/state        tree, masks, canvas metadata
    POST /api/stroke       paint a soft stroke into a layer raster
    POST /api/node         change opacity / blend / clipping / isolation
    POST /api/layers       add/delete/reorder layers or groups
    POST /api/mask         paint into a shared mask
    POST /api/evaluate     run the evaluator, return composite PNG + trace
    GET  /api/pixel?x&y    per-stage RGBA evaluation for one pixel
    POST /api/save         JSON project download
    POST /api/export       flattened PNG download

Errors are returned as JSON ``{"error": ...}`` with a non-200 status and are
shown in the window's error strip by the frontend.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .backends.accelerated import AcceleratedBackend
from .backends.reference import ReferenceBackend
from .demo import build_demo_project
from .engine import Evaluator
from .model import Group, Layer, LayerPaintError
from .raster import Raster, write_png
from . import serialization
import base64
import io
import os

WEB_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "web"))


class PaintingSession:
    """Holds the live project and its evaluator behind a lock."""

    def __init__(self, project=None, backend="ref"):
        self.project = project or build_demo_project()
        self._lock = threading.RLock()
        self.set_backend(backend)

    def set_backend(self, name):
        with self._lock:
            backend = AcceleratedBackend() if name == "acc" else ReferenceBackend()
            self.evaluator = Evaluator(self.project, backend)
            self.backend_name = name

    def lock(self):
        return self._lock


def _node_payload(node):
    data = {
        "uid": node.uid,
        "name": node.name,
        "kind": node.kind,
        "opacity": node.opacity,
        "blend_mode": node.blend_mode,
        "clipped": node.clipped,
        "mask_id": node.mask_id,
    }
    if isinstance(node, Group):
        data["isolated"] = node.isolated
        data["children"] = [_node_payload(c) for c in node.children]
        data["empty"] = node.is_empty()
    return data


class Handler(BaseHTTPRequestHandler):
    session = None  # injected by make_server

    def log_message(self, fmt, *args):
        return  # quiet; errors go through the API payload

    # ------------------------------------------------------------- transport

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_png(self, raster, status=200, download_name=None):
        buf = io.BytesIO()
        write_png(buf, raster)
        body = buf.getvalue()
        self.send_response(status)
        self.send_header("Content-Type", "image/png")
        if download_name:
            self.send_header(
                "Content-Disposition",
                'attachment; filename="%s"' % download_name)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _error(self, exc, status=400):
        self._send_json({"error": str(exc)}, status=status)

    # ----------------------------------------------------------------- routes

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                return self._serve_index()
            if parsed.path == "/api/state":
                return self._get_state()
            if parsed.path == "/api/pixel":
                return self._get_pixel(parse_qs(parsed.query))
            self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # surfaced in the painting window
            self._error(exc, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            # Each handler takes the session lock itself; wrapping the whole
            # dispatch in a lock would deadlock because handlers re-enter it.
            if parsed.path == "/api/stroke":
                return self._stroke(payload)
            if parsed.path == "/api/node":
                return self._update_node(payload)
            if parsed.path == "/api/layers":
                return self._edit_layers(payload)
            if parsed.path == "/api/mask":
                return self._paint_mask(payload)
            if parsed.path == "/api/evaluate":
                return self._evaluate(payload)
            if parsed.path == "/api/save":
                return self._save()
            if parsed.path == "/api/export":
                return self._export(payload)
            if parsed.path == "/api/backend":
                self.session.set_backend(payload.get("backend", "ref"))
                return self._send_json({"backend": self.session.backend_name})
            if parsed.path == "/api/reset":
                with self.session.lock():
                    self.session.project = build_demo_project()
                    self.session.set_backend(self.session.backend_name)
                return self._get_state()
            self._send_json({"error": "not found"}, 404)
        except LayerPaintError as exc:
            self._error(exc, 422)
        except (KeyError, ValueError) as exc:
            self._error(exc, 400)
        except Exception as exc:
            self._error(exc, 500)

    # ------------------------------------------------------------- GET impl

    def _serve_index(self):
        path = os.path.join(WEB_DIR, "index.html")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_state(self):
        p = self.session.project
        self._send_json({
            "width": p.width,
            "height": p.height,
            "backend": self.session.backend_name,
            "root": _node_payload(p.root),
            "masks": sorted(p.masks.keys()),
            "blend_modes": sorted(_all_modes()),
        })

    def _get_pixel(self, query):
        x = int(query["x"][0])
        y = int(query["y"][0])
        with self.session.lock():
            stages = self.session.evaluator.pixel_trace(x, y)
        self._send_json({"x": x, "y": y, "stages": stages})

    # ------------------------------------------------------------ POST impl

    def _stroke(self, payload):
        node = self._find(int(payload["uid"]))
        if not isinstance(node, Layer):
            raise LayerPaintError("strokes can only be applied to layers")
        node.raster.stroke(
            [(p["x"], p["y"]) for p in payload["points"]],
            tuple(payload["color"]),
            float(payload["radius"]),
            float(payload.get("hardness", 0.5)),
        )
        self.session.evaluator.touch_node(node.uid)
        self._send_json({"ok": True})

    def _paint_mask(self, payload):
        mask_id = payload["mask_id"]
        if mask_id not in self.session.project.masks:
            raise KeyError("unknown mask %r" % mask_id)
        mask = self.session.project.masks[mask_id]
        # White adds coverage, black removes; stored on the red channel.
        mask.stroke(
            [(p["x"], p["y"]) for p in payload["points"]],
            tuple(payload["color"]),
            float(payload["radius"]),
            float(payload.get("hardness", 0.5)),
        )
        rect = payload.get("rect")
        self.session.evaluator.touch_mask(
            mask_id, tuple(rect) if rect else None)
        self._send_json({"ok": True})

    def _update_node(self, payload):
        node = self._find(int(payload["uid"]))
        if "opacity" in payload:
            node.opacity = float(payload["opacity"])
        if "blend_mode" in payload:
            node.blend_mode = payload["blend_mode"]
        if "clipped" in payload:
            node.clipped = bool(payload["clipped"])
        if "isolated" in payload and isinstance(node, Group):
            node.isolated = bool(payload["isolated"])
        if "name" in payload:
            node.name = str(payload["name"])
        self.session.project.validate()
        self.session.evaluator.touch_node(node.uid)
        self._send_json({"ok": True})

    def _edit_layers(self, payload):
        action = payload["action"]
        p = self.session.project
        if action == "add-layer":
            parent = self._find_group(int(payload["parent_uid"]))
            raster = Raster(p.width, p.height)
            layer = Layer(payload.get("name", "layer %d" % p._next_uid),
                          raster, blend_mode=payload.get("blend_mode", "normal"),
                          opacity=float(payload.get("opacity", 1.0)),
                          clipped=bool(payload.get("clipped", False)))
            parent.add(layer)
            p._assign_uids(p.root)
            self.session.evaluator.sync_project_nodes()
            self.session.evaluator.touch_node(parent.uid)
            return self._send_json({"uid": layer.uid})
        if action == "add-group":
            parent = self._find_group(int(payload["parent_uid"]))
            group = Group(payload.get("name", "group"),
                          isolated=bool(payload.get("isolated", True)))
            parent.add(group)
            p._assign_uids(p.root)
            self.session.evaluator.sync_project_nodes()
            self.session.evaluator.touch_node(parent.uid)
            return self._send_json({"uid": group.uid})
        if action == "delete":
            node = self._find(int(payload["uid"]))
            if node is p.root:
                raise LayerPaintError("cannot delete the root group")
            node.parent.children.remove(node)
            self.session.evaluator.sync_project_nodes()
            self.session.evaluator.invalidate_all()
            return self._send_json({"ok": True})
        if action == "move":
            node = self._find(int(payload["uid"]))
            target_parent = self._find_group(int(payload["parent_uid"]))
            index = int(payload["index"])
            node.parent.children.remove(node)
            target_parent.children.insert(index, node)
            node.parent = target_parent
            self.session.evaluator.sync_project_nodes()
            self.session.evaluator.invalidate_all()
            return self._send_json({"ok": True})
        raise ValueError("unknown layers action %r" % action)

    def _evaluate(self, payload):
        with self.session.lock():
            if payload.get("backend"):
                self.session.set_backend(payload["backend"])
            raster, trace = self.session.evaluator.evaluate()
            stages = [{
                "uid": s["uid"], "name": s["name"], "kind": s["kind"],
                "blend_mode": s["blend_mode"], "opacity": s["opacity"],
                "isolated": s["isolated"], "clipped": s["clipped"],
            } for s in trace]
        # PNG as base64 JSON so a single endpoint carries image + trace.
        buf = io.BytesIO()
        write_png(buf, raster)
        self._send_json({
            "png_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "stages": stages,
            "log": self.session.evaluator.log,
        })

    def _save(self):
        data = serialization.to_dict(self.session.project)
        self._send_json({"project": data})

    def _export(self, payload):
        raster, _ = self.session.evaluator.evaluate()
        self._send_png(raster, download_name=payload.get("name", "composite.png"))

    # ---------------------------------------------------------------- helpers

    def _find(self, uid):
        return self.session.project.get(uid)

    def _find_group(self, uid):
        node = self._find(uid)
        if not isinstance(node, Group):
            raise LayerPaintError("node %r is not a group" % node.name)
        return node


def _all_modes():
    from .blend import BLEND_MODES
    return BLEND_MODES


def make_server(host="127.0.0.1", port=0, project=None, backend="ref"):
    session = PaintingSession(project, backend)
    Handler.session = session
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd, session


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Run the painting window server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--backend", choices=["ref", "acc"], default="ref")
    args = parser.parse_args(argv)
    httpd, _ = make_server(args.host, args.port, backend=args.backend)
    print("Painting window: http://%s:%d/" % (args.host, args.port))
    print("Close with Ctrl+C.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

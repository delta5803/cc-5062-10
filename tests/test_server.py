"""End-to-end tests for the painting window HTTP API.

The browser itself is only a view; these tests exercise every endpoint the
frontend calls against a real in-process server and confirm compositing is
done by the Python evaluator, including error reporting through the API.
"""

import base64
import io
import json
import os
import sys
import threading
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.raster import read_png  # noqa: E402
from layerpaint.server import make_server  # noqa: E402


class ServerHarness:
    def __init__(self):
        self.httpd, self.session = make_server("127.0.0.1", 0)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class TestServerAPI(unittest.TestCase):
    def setUp(self):
        self.h = ServerHarness()

    def tearDown(self):
        self.h.stop()

    def test_index_served(self):
        with urllib.request.urlopen(self.h.base + "/") as resp:
            body = resp.read().decode("utf-8")
        self.assertIn("图层合成绘画窗口", body)

    def test_state_reports_tree_and_modes(self):
        status, data = self.h.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(data["width"], 32)
        self.assertIn("multiply", data["blend_modes"])
        self.assertIn("vignette", data["masks"])
        names = {n["name"] for n in _walk(data["root"])}
        self.assertIn("pass-through-wash", names)
        self.assertIn("empty-notes", names)

    def test_evaluate_returns_real_png_and_trace(self):
        status, data = self.h.post("/api/evaluate", {"backend": "ref"})
        self.assertEqual(status, 200)
        png = base64.b64decode(data["png_base64"])
        raster = read_png(io.BytesIO(png))
        self.assertEqual((raster.width, raster.height), (32, 32))
        # Something was actually painted: not every pixel equals the paper.
        self.assertTrue(any(v != 245 for v in raster.data[0::4]))
        self.assertTrue(any(s["kind"] == "group" for s in data["stages"]))

    def test_backend_switch_keeps_parity(self):
        _, ref = self.h.post("/api/evaluate", {"backend": "ref"})
        _, acc = self.h.post("/api/evaluate", {"backend": "acc"})
        self.assertEqual(ref["png_base64"], acc["png_base64"])

    def test_stroke_then_recompute_changes_output(self):
        _, before = self.h.post("/api/evaluate", {})
        target = _find_layer(self.h.session.project, "speckle")
        status, _ = self.h.post("/api/stroke", {
            "uid": target.uid,
            "points": [{"x": 2, "y": 2}, {"x": 6, "y": 6}],
            "color": [255, 0, 255, 255], "radius": 3, "hardness": 0.5,
        })
        self.assertEqual(status, 200)
        _, after = self.h.post("/api/evaluate", {})
        self.assertNotEqual(before["png_base64"], after["png_base64"])

    def test_node_update_and_validation_error(self):
        iso_group = next(
            n for n in self.h.session.project.all_nodes()
            if n.name == "isolated-painting")
        status, _ = self.h.post("/api/node",
                                {"uid": iso_group.uid, "opacity": 0.25})
        self.assertEqual(status, 200)
        # Pass-through requires opacity 1 and normal blend; the now-0.25
        # opacity makes switching to pass-through invalid and must be reported.
        status, err = self.h.post("/api/node", {
            "uid": iso_group.uid, "isolated": False})
        self.assertEqual(status, 422)
        self.assertIn("error", err)

    def test_pixel_trace_returns_stage_values(self):
        status, data = self.h.get("/api/pixel?x=16&y=16")
        self.assertEqual(status, 200)
        self.assertTrue(len(data["stages"]) >= 3)
        for stage in data["stages"]:
            self.assertEqual(len(stage["rgba_bytes"]), 4)
            self.assertEqual(len(stage["rgba_unit"]), 4)

    def test_add_and_delete_layer(self):
        root_uid = self.h.session.project.root.uid
        status, data = self.h.post("/api/layers", {
            "action": "add-layer", "parent_uid": root_uid, "name": "new"})
        self.assertEqual(status, 200)
        new_uid = data["uid"]
        status, _ = self.h.post("/api/layers",
                                {"action": "delete", "uid": new_uid})
        self.assertEqual(status, 200)
        with self.assertRaises(KeyError):
            self.h.session.project.get(new_uid)

    def test_mask_paint_uses_dependency_path(self):
        status, _ = self.h.post("/api/mask", {
            "mask_id": "vignette",
            "points": [{"x": 10, "y": 10}, {"x": 20, "y": 20}],
            "color": [0, 0, 0, 180], "radius": 3, "hardness": 0.4,
            "rect": [8, 8, 22, 22],
        })
        self.assertEqual(status, 200)
        _, data = self.h.post("/api/evaluate", {})
        mask_events = [row for row in data["log"] if row[0] == "mask-apply"]
        self.assertTrue(mask_events)

    def test_export_returns_png_download(self):
        req = urllib.request.Request(
            self.h.base + "/api/export",
            data=json.dumps({"name": "composite.png"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.headers["Content-Type"], "image/png")
            self.assertIn("attachment", resp.headers["Content-Disposition"])
            png = resp.read()
        raster = read_png(io.BytesIO(png))
        self.assertEqual((raster.width, raster.height), (32, 32))

    def test_unknown_route_is_json_error(self):
        status, err = self.h.post("/api/nope", {})
        self.assertEqual(status, 404)
        self.assertIn("error", err)


def _walk(node):
    yield node
    for child in node.get("children", []):
        yield from _walk(child)


def _find_layer(project, name):
    for node in project.all_nodes():
        if node.name == name:
            return node
    raise KeyError(name)


if __name__ == "__main__":
    unittest.main()

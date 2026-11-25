"""Hand-built demo projects used by the CLI, server and test fixtures.

The demo deliberately mixes the features the task calls out: soft
semi-transparent strokes, an isolated and a pass-through group sharing a mask,
a clipping chain with an interruption, nested groups and an empty group.
"""

from .model import Group, Layer, Project
from .raster import Raster


def _stroke_layer(name, strokes, size=32, **kw):
    raster = Raster(size, size)
    for points, color, radius, hardness in strokes:
        raster.stroke(points, color, radius, hardness)
    return Layer(name, raster, **kw)


def build_demo_project(size=32):
    paper = Raster(size, size, bytearray([245, 242, 235, 255] * size * size))

    soft_red = _stroke_layer("soft-red", [
        ([(6, 8), (14, 18), (24, 22)], (210, 40, 40, 130), 4.0, 0.35),
    ], size, blend_mode="multiply")

    blue_wash = _stroke_layer("blue-wash", [
        ([(4, 24), (16, 14), (28, 8)], (30, 70, 220, 90), 5.0, 0.2),
    ], size)

    # Clipping chain: highlight confined to the soft-red footprint, then a
    # non-clipped paper speckle breaks the chain.
    highlight = _stroke_layer("highlight", [
        ([(8, 10), (20, 20)], (255, 240, 180, 200), 2.0, 0.6),
    ], size, clipped=True)
    speckle = _stroke_layer("speckle", [
        ([(26, 26)], (60, 60, 60, 255), 1.5, 1.0),
    ], size)

    mask = Raster(size, size)
    # Radial-ish grayscale mask: opaque center fading to transparent edges.
    for y in range(size):
        for x in range(size):
            dx = x - size / 2
            dy = y - size / 2
            d = (dx * dx + dy * dy) ** 0.5
            v = max(0, min(255, int(255 * (1.0 - d / (size * 0.75)))))
            i = (y * size + x) * 4
            mask.data[i:i + 4] = bytes([v, v, v, 255])

    isolated_group = Group(
        "isolated-painting",
        [soft_red, highlight],
        isolated=True,
        mask_id="vignette",
    )
    pass_group = Group(
        "pass-through-wash",
        [blue_wash],
        isolated=False,
        mask_id="vignette",
    )
    empty = Group("empty-notes", [])
    nested = Group("nested", [empty, speckle], isolated=True)

    root = Group("root", [
        Layer("paper", paper),
        isolated_group,
        pass_group,
        nested,
    ])
    project = Project(size, size, root)
    project.add_mask("vignette", mask)
    return project

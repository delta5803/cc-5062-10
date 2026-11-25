"""Command-line helper for layer compositing (the painting window is the
primary graphical entry point; see docs/ARCHITECTURE.md).

Subcommands:
  render   <project.json> <out.png> [--backend ref|acc] [--rect x0 y0 x1 y1]
  demo     <out.png>                     build and render the bundled demo
  pixel    <project.json> <x> <y>        print the per-stage evaluation trace
  compare  <project.json>                CPU reference vs accelerated parity
"""

import argparse
import sys

from .backends.accelerated import AcceleratedBackend
from .backends.reference import ReferenceBackend
from .demo import build_demo_project
from .engine import Evaluator
from .raster import write_png
from .serialization import load

BACKENDS = {
    "ref": ReferenceBackend,
    "acc": AcceleratedBackend,
}


def _make_evaluator(project, backend_name):
    return Evaluator(project, BACKENDS[backend_name]())


def cmd_render(args):
    project = load(args.project)
    ev = _make_evaluator(project, args.backend)
    if args.rect:
        ev.evaluate()  # establish a full root raster to patch the rect into
        raster, _ = ev.reevaluate_dirty_rect(tuple(args.rect))
    else:
        raster, _ = ev.evaluate()
    write_png(args.output, raster)
    print("rendered %dx%d -> %s (%s)"
          % (project.width, project.height, args.output, args.backend))


def cmd_demo(args):
    project = build_demo_project(size=args.size)
    raster, _ = _make_evaluator(project, args.backend).evaluate()
    write_png(args.output, raster)
    print("demo rendered -> %s" % args.output)


def cmd_pixel(args):
    project = load(args.project)
    stages = Evaluator(project).pixel_trace(args.x, args.y)
    for s in stages:
        print("%-20s %-6s blend=%-11s op=%.2f iso=%s clip=%s rgba=%s unit=%s"
              % (s["name"], s["kind"], s["blend_mode"], s["opacity"],
                 s["isolated"], s["clipped"],
                 s["rgba_bytes"], s["rgba_unit"]))


def cmd_compare(args):
    project = load(args.project)
    ref, _ = Evaluator(project, ReferenceBackend()).evaluate()
    acc, _ = Evaluator(project, AcceleratedBackend()).evaluate()
    worst = max(abs(a - b) for a, b in zip(ref.data, acc.data))
    print("max per-channel byte difference: %d" % worst)
    if worst > 1:
        print("BACKEND MISMATCH", file=sys.stderr)
        return 1
    print("backends agree within quantization tolerance")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="layerpaint",
        description="Layer compositing CLI (graphical painting window is the "
                    "primary entry point).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_render = sub.add_parser("render", help="render a saved project to PNG")
    p_render.add_argument("project")
    p_render.add_argument("output")
    p_render.add_argument("--backend", choices=list(BACKENDS), default="ref")
    p_render.add_argument("--rect", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"))
    p_render.set_defaults(func=cmd_render)

    p_demo = sub.add_parser("demo", help="render the bundled demo project")
    p_demo.add_argument("output")
    p_demo.add_argument("--size", type=int, default=32)
    p_demo.add_argument("--backend", choices=list(BACKENDS), default="ref")
    p_demo.set_defaults(func=cmd_demo)

    p_pixel = sub.add_parser("pixel", help="per-layer pixel evaluation trace")
    p_pixel.add_argument("project")
    p_pixel.add_argument("x", type=int)
    p_pixel.add_argument("y", type=int)
    p_pixel.set_defaults(func=cmd_pixel)

    p_cmp = sub.add_parser("compare", help="reference vs accelerated parity")
    p_cmp.add_argument("project")
    p_cmp.set_defaults(func=cmd_compare)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())

"""Group-evaluation cost benchmarks.

The benchmark measures the cost of evaluating deeply nested isolated groups
under soft-edge brush load, and reports three honest numbers for every run:

* **cost decomposition** - per-kind evaluator events (placements, frozen
  splices, recomputed rectangles, skipped backdrop clones, ...) and the number
  of backend composite/clip calls;
* **compositing error** - the maximum per-channel byte difference between the
  optimized dirty-rect result and a from-scratch full reference evaluation
  (must be 0 for the reference backend; the accelerated backend is allowed the
  same 1-byte quantization tolerance as the rest of the suite);
* **buffer peak** - live/allocated temporary raster bytes tracked by the
  evaluator's :class:`~layerpaint.engine.BufferTracker`.

Nothing here is a synthetic micro-score: every "speed" claim is backed by an
exact pixel comparison against a full reference run, so a benchmark cannot
report a win while producing wrong pixels.  Run it with::

    PYTHONPATH=src python3 -m layerpaint.benchmark
    PYTHONPATH=src python3 -m layerpaint.benchmark --sizes 16 32 64 --depth 12
"""

import argparse
import time

from .backends.accelerated import AcceleratedBackend
from .backends.reference import ReferenceBackend
from .engine import Evaluator
from .model import Group, Layer, Project
from .raster import Raster

# A dirty rect that covers the edited soft stroke plus its brush radius, so
# the rect genuinely bounds the antialiased influence (callers are responsible
# for declaring a covering rect; the evaluator never invents one).  The margin
# is a fraction of the canvas rather than a fixed pixel count, so small
# canvases still get a rect that covers the whole soft stamp.
def _covering_rect(size, margin_fraction=0.28):
    margin = max(2, int(round(size * margin_fraction)))
    return (margin, margin, size - margin, size - margin)


def build_nested_project(depth, size):
    """Deep isolated groups wrapping one soft-brush leaf, plus an unchanged
    sibling layer used to measure redundant refresh of neighbors."""
    leaf_ink = Raster(size, size)
    leaf_ink.stroke(
        [(3, 3), (size - 4, size - 4)], (220, 40, 40, 180),
        radius=max(3.0, size / 8.0), hardness=0.3)
    node = Layer("leaf", leaf_ink, blend_mode="multiply", opacity=0.85)
    for d in range(depth):
        node = Group(
            "g%d" % d, [node], isolated=True, opacity=0.95,
            blend_mode="screen" if d % 2 else "normal")
    neighbor_ink = Raster(size, size)
    neighbor_ink.stroke(
        [(2, size - 3), (size - 3, 2)], (40, 60, 220, 150),
        radius=max(4.0, size / 6.0), hardness=0.2)
    root = Group("root", [
        Layer("paper", Raster(
            size, size, bytearray([245, 242, 235, 255]) * size * size)),
        node,
        Layer("neighbor", neighbor_ink),
    ])
    return Project(size, size, root)


def build_clip_chain_project(size, chain):
    """A clipping base serving many clipped soft-brush siblings."""
    base_ink = Raster(size, size)
    base_ink.stroke(
        [(4, 4), (size - 5, size - 5)], (200, 30, 30, 255),
        radius=max(4.0, size / 7.0), hardness=0.5)
    children = [Layer("base", base_ink)]
    for k in range(chain):
        ink = Raster(size, size)
        ink.stroke(
            [(3, 3), (size - 4, size - 6)], (30, 60, 220, 200),
            radius=max(3.0, size / 9.0), hardness=0.3)
        children.append(Layer(
            "clip%d" % k, ink, clipped=True,
            blend_mode="screen", opacity=0.8))
    return Project(size, size, Group("root", children))


def max_byte_diff(a, b):
    return max(abs(x - y) for x, y in zip(a.data, b.data))


def _edit_leaf(project):
    leaf = next(n for n in project.all_nodes() if n.name == "leaf")
    size = project.width
    leaf.raster.stroke(
        [(size // 3, size // 3), (2 * size // 3, 2 * size // 3)],
        (0, 0, 200, 200), radius=max(2.0, size / 12.0), hardness=0.5)
    return leaf


def _fresh_edit(depth, size):
    """A freshly built nested project with one edit already applied."""
    project = build_nested_project(depth, size)
    leaf = _edit_leaf(project)
    return project, leaf


def benchmark_nested(depth, size, backend, repeats=10):
    """Cost of a re-evaluation inside a deep isolated chain.

    The dirty rect spans the full canvas so the comparison against the
    from-scratch reference is exact regardless of how far a soft stamp
    antialiases; sub-rectangle reuse is exercised separately by
    :func:`benchmark_full_vs_dirty` with a covering rect.
    """
    rect = (0, 0, size, size)
    timings = []
    last_error = None
    last_cost = None
    for _ in range(repeats):
        project = build_nested_project(depth, size)
        evaluator = Evaluator(project, backend)
        evaluator.evaluate()  # establish the full-run reuse baseline
        leaf = _edit_leaf(project)
        evaluator.touch_node(leaf.uid, rect=rect)
        start = time.perf_counter()
        partial, _ = evaluator.reevaluate_dirty_rect(rect)
        timings.append((time.perf_counter() - start) * 1000.0)
        # Exact correctness against an independent from-scratch reference.
        reference, _ = Evaluator(
            project, ReferenceBackend()).evaluate()
        last_error = max_byte_diff(partial, reference)
        last_cost = evaluator.cost_breakdown()
    return {
        "case": "nested-groups",
        "depth": depth,
        "size": size,
        "backend": backend.name,
        "repeats": repeats,
        "avg_ms": round(sum(timings) / len(timings), 4),
        "max_ms": round(max(timings), 4),
        "max_composite_error_bytes": last_error,
        "cost": last_cost,
    }


def benchmark_full_vs_dirty(depth, size, backend):
    """Same edit applied to two identical projects: full evaluation vs a
    dirty-rect re-evaluation after a warm full run, with the pixel error
    between the two."""
    rect = _covering_rect(size)

    full_project, _ = _fresh_edit(depth, size)
    full_evaluator = Evaluator(full_project, backend)
    t0 = time.perf_counter()
    full_raster, _ = full_evaluator.evaluate()
    full_ms = (time.perf_counter() - t0) * 1000.0

    dirty_project, dirty_leaf = _fresh_edit(depth, size)
    dirty_evaluator = Evaluator(dirty_project, backend)
    dirty_evaluator.evaluate()  # warm baseline for reuse
    dirty_evaluator.touch_node(dirty_leaf.uid, rect=rect)
    t0 = time.perf_counter()
    dirty_raster, _ = dirty_evaluator.reevaluate_dirty_rect(rect)
    dirty_ms = (time.perf_counter() - t0) * 1000.0

    return {
        "case": "full-vs-dirty",
        "depth": depth,
        "size": size,
        "backend": backend.name,
        "full_ms": round(full_ms, 4),
        "dirty_rect_ms": round(dirty_ms, 4),
        "speedup_x": round(full_ms / dirty_ms, 3) if dirty_ms else None,
        "max_composite_error_bytes": max_byte_diff(full_raster, dirty_raster),
        "dirty_cost": dirty_evaluator.cost_breakdown(),
        "full_cost": full_evaluator.cost_breakdown(),
    }


def benchmark_clip_chain(size, chain, backend):
    """Clipping chain: alpha-footprint reuse and exact output."""
    project = build_clip_chain_project(size, chain)
    evaluator = Evaluator(project, backend)
    t0 = time.perf_counter()
    raster, _ = evaluator.evaluate()
    full_ms = (time.perf_counter() - t0) * 1000.0
    reference, _ = Evaluator(project, ReferenceBackend()).evaluate()
    return {
        "case": "clip-chain",
        "chain": chain,
        "size": size,
        "backend": backend.name,
        "full_ms": round(full_ms, 4),
        "max_composite_error_bytes": max_byte_diff(raster, reference),
        "cost": evaluator.cost_breakdown(),
    }


def run_all(sizes, depth, chain, repeats):
    rows = []
    for size in sizes:
        rows.append(benchmark_nested(depth, size, ReferenceBackend(), repeats))
        rows.append(benchmark_nested(depth, size, AcceleratedBackend(), repeats))
        rows.append(benchmark_full_vs_dirty(depth, size, ReferenceBackend()))
        rows.append(benchmark_clip_chain(size, chain, ReferenceBackend()))
    return rows


def _format_row(row):
    cost = row.get("cost") or row.get("dirty_cost")
    events = cost["events"] if cost else {}
    buffers = cost["buffers"] if cost else {}
    head = "%-13s size=%-3d backend=%-15s" % (
        row["case"], row["size"], row["backend"])
    metrics = "err=%dB peak=%sB frozen=%s clone_skip=%s" % (
        row["max_composite_error_bytes"],
        buffers.get("peak_live_bytes", "-"),
        events.get("frozen-splice", 0),
        events.get("clone-skipped", 0),
    )
    if "avg_ms" in row:
        timing = "avg=%.3fms" % row["avg_ms"]
    elif "full_ms" in row and "dirty_rect_ms" in row:
        timing = "full=%.3fms dirty=%.3fms speedup=%sx" % (
            row["full_ms"], row["dirty_rect_ms"], row["speedup_x"])
    else:
        timing = "full=%.3fms" % row["full_ms"]
    return "%s %s %s" % (head, timing, metrics)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="layerpaint.benchmark",
        description="Group-evaluation cost benchmarks with exact pixel checks.")
    parser.add_argument("--sizes", nargs="+", type=int, default=[16, 32, 64])
    parser.add_argument("--depth", type=int, default=10)
    parser.add_argument("--chain", type=int, default=6)
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args(argv)

    rows = run_all(args.sizes, args.depth, args.chain, args.repeats)
    worst_error = max(r["max_composite_error_bytes"] for r in rows)
    for row in rows:
        print(_format_row(row))
    print("-" * 72)
    print("worst compositing error across cases: %d byte(s)" % worst_error)
    # The reference backend must be byte-exact and the accelerated backend is
    # allowed the same 1-byte quantization tolerance as the parity test suite.
    # Any larger error invalidates the run so a speedup can never be reported
    # on top of wrong pixels.
    failures = [
        r for r in rows
        if r["max_composite_error_bytes"]
        > (0 if r["backend"] == ReferenceBackend.name else 1)
    ]
    if failures:
        print("PIXEL MISMATCH BEYOND TOLERANCE - benchmark result is invalid")
        for r in failures:
            print("  %s size=%s err=%dB"
                  % (r["case"], r["size"], r["max_composite_error_bytes"]))
        return 1
    print("reference outputs byte-exact; accelerated within 1-byte tolerance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

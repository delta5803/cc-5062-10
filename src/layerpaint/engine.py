"""Layer compositing evaluator.

The evaluator turns a :class:`~layerpaint.model.Project` tree into pixels by
the W3C rules, with three pieces of machinery the task requires explicitly:

1. **Isolated vs pass-through groups.** An isolated group composites its
   children onto a transparent black backdrop first, then enters the parent
   backdrop once.  A pass-through group has no own backdrop; its children are
   evaluated against the *parent's* running backdrop.  Opacity below 1 or a
   non-normal blend mode forces isolation, matching the W3C stacking model -
   group opacity is therefore never faked by flattening first.

2. **Clipping chains.** A clipped node is limited to the alpha of the nearest
   non-clipped sibling below it (its clipping base).  The chain is broken by
   any non-clipped sibling, which becomes the new base.

3. **Dependency-driven recompute ranges.** Each cached local render remembers
   the node/mask versions it was built from and rectangles changed since.  A
   shared mask invalidates the isolated group's self-contained cache only
   inside the mask's dirty rect, while a pass-through group (which holds no own
   backdrop) forces a recompute of its parent's accumulated render over that
   rect - different cache boundaries, decided by read/write dependencies.

Optimizations for deep nesting (all byte-for-byte equivalent to a full
layer-by-layer expansion, verified by tests):

* **Lazy backdrop snapshots.** A node only needs the pre-placement backdrop
  snapshot when it (or a later sibling) is clipped to a base; otherwise no
  full-canvas clone is allocated.
* **Clipping-base alpha memoization.** A node's masked footprint (the
  clipping alpha) is cached across runs and keyed on its node/mask versions
  and rect, so an unchanged base is rendered and masked once even when the
  clipping chain is rebuilt on a later evaluation.
* **Frozen-subtree splice.** On a dirty-rect re-evaluation an isolated group
  whose whole subtree is untouched by the tracked edits, whose mask/placement
  fields are unchanged and whose incoming backdrop rectangle is empty splices
  that rectangle from its own previous placed raster instead of re-running
  every descendant - a reusable, dependency-keyed composite boundary rather
  than a blind flatten.
"""

import weakref

from .backends.reference import ReferenceBackend
from .model import Group, Layer, LayerPaintError

FULL = None  # whole-canvas rect sentinel


class CacheEntry:
    def __init__(self, raster, key):
        self.raster = raster
        self.key = key
        self.dirty_rects = []


class BufferTracker:
    """Counts temporary raster buffers and the live/peak footprint.

    Buffers are held through weak references, so the tracker never extends a
    raster's lifetime: the peak is measured from buffers that the evaluator
    still actually holds (CPython refcounting frees intermediates immediately).
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._refs = []
        self.live_bytes = 0
        self.peak_bytes = 0
        self.allocations = 0
        self.allocated_bytes = 0

    def track(self, raster):
        size = len(raster.data)
        self.allocations += 1
        self.allocated_bytes += size
        alive = []
        live = 0
        for ref in self._refs:
            r = ref()
            if r is not None:
                alive.append(ref)
                live += len(r.data)
        live += size
        self._refs = alive
        self._refs.append(weakref.ref(raster))
        self.live_bytes = live
        if live > self.peak_bytes:
            self.peak_bytes = live
        return raster


class Evaluator:
    def __init__(self, project, backend=None):
        self.project = project
        self.backend = backend or ReferenceBackend()
        self._buffer_tracker = BufferTracker()
        self._install_tracking()
        self._cache = {}
        self._root_raster = None
        # Each node's own output raster at the end of the previous run (the
        # raster placed into its parent, *not* the full root raster, so it
        # never carries later siblings painted on top).  Frozen splices copy
        # from here.
        self._last_placed = {}
        self._node_versions = {n.uid: 0 for n in project.all_nodes()}
        self._mask_versions = {mid: 0 for mid in project.masks}
        # Versions at the end of the previous completed run; the frozen-subtree
        # test compares current versions against this baseline.
        self._last_run_node_versions = dict(self._node_versions)
        self._last_run_mask_versions = dict(self._mask_versions)
        self.log = []
        # Per-evaluation-run reusable state (stats, frozen mode).
        self._run_id = 0
        # Clipping-base alpha footprints memoized across runs; the key carries
        # the node/mask versions and rect, so any edit invalidates them.
        self._alpha_cache = {}
        self.stats = self._fresh_stats()
        # Bounded history of tracked edits, used by the "why redraw" lookup.
        self.edits = []
        self._edit_seq = 0
        self._frozen_mode = False
        self._frozen_rect = FULL
        # Uids on the ancestor chain of nodes edited since the last full run;
        # such groups carry the dirty rectangle and can never be frozen.
        self._edited_ancestor_uids = set()

    # ------------------------------------------------------------- tracking

    def _fresh_stats(self):
        return {
            "cache-hit": 0,
            "rebuild": 0,
            "recompute-rect": 0,
            "frozen-splice": 0,
            "alpha-hit": 0,
            "place": 0,
            "passthrough": 0,
            "mask-apply": 0,
            "clone-skipped": 0,
            "composite-calls": 0,
            "clip-calls": 0,
            "buffer-allocations": 0,
            "buffer-allocated-bytes": 0,
            "buffer-peak-bytes": 0,
        }

    def _install_tracking(self):
        """Wrap backend raster-producing calls so every buffer is metered."""
        tracker = self._buffer_tracker
        backend = self.backend
        orig_empty = backend.empty_raster
        orig_composite = backend.composite
        orig_mask = backend.apply_mask
        orig_clip = backend.clip_lerp

        def empty_raster(width, height):
            return tracker.track(orig_empty(width, height))

        def composite(backdrop, source, mode, opacity, rect=None):
            self.stats["composite-calls"] += 1
            return tracker.track(
                orig_composite(backdrop, source, mode, opacity, rect))

        def apply_mask(raster, mask, rect=None):
            self.stats["mask-apply"] += 1
            return tracker.track(orig_mask(raster, mask, rect))

        def clip_lerp(backdrop, over_render, base_alpha, rect=None):
            self.stats["clip-calls"] += 1
            return tracker.track(
                orig_clip(backdrop, over_render, base_alpha, rect))

        backend.empty_raster = empty_raster
        backend.composite = composite
        backend.apply_mask = apply_mask
        backend.clip_lerp = clip_lerp

    def _begin_run(self, frozen=False, rect=FULL):
        self._run_id += 1
        self._frozen_mode = frozen
        self._frozen_rect = rect
        self.log = []
        self.stats = self._fresh_stats()
        self._buffer_tracker.reset()

    def _end_run(self, trace=None):
        if not self._frozen_mode and trace is not None:
            # A full run is the trustworthy baseline: remember each node's own
            # placed raster so a later dirty-rect run can splice frozen ones.
            self._last_placed = {
                stage["uid"]: stage["raster"] for stage in trace
            }
            # The new baseline already reflects every edit, so no ancestor is
            # "dirty relative to baseline" until the next tracked edit.
            self._edited_ancestor_uids = set()
        self._last_run_node_versions = dict(self._node_versions)
        self._last_run_mask_versions = dict(self._mask_versions)
        self._frozen_mode = False
        self.stats["buffer-allocations"] = self._buffer_tracker.allocations
        self.stats["buffer-allocated-bytes"] = \
            self._buffer_tracker.allocated_bytes
        self.stats["buffer-peak-bytes"] = self._buffer_tracker.peak_bytes

    def cost_breakdown(self):
        """Group-evaluation cost decomposition for the last run."""
        return {
            "events": {k: v for k, v in self.stats.items()
                       if not k.startswith("buffer-")},
            "buffers": {
                "allocations": self.stats["buffer-allocations"],
                "allocated_bytes": self.stats["buffer-allocated-bytes"],
                "peak_live_bytes": self.stats["buffer-peak-bytes"],
            },
        }

    # ------------------------------------------------------------- mutations

    def touch_node(self, uid, rect=FULL):
        """Record an edit to a node (raster/opacity/blend/tree position)."""
        if uid not in self._node_versions:
            raise KeyError("unknown node uid %r" % uid)
        self._node_versions[uid] += 1
        node = self.project.get(uid)
        cur = node
        while cur is not None:
            self._mark_dirty(cur.uid, rect)
            self._edited_ancestor_uids.add(cur.uid)
            cur = cur.parent
        self._record_edit({
            "seq": self._edit_seq, "type": "node", "uid": uid,
            "name": node.name, "rect": rect,
        })

    def touch_mask(self, mask_id, rect=FULL):
        """Propagate a mask edit along the real read/write dependencies.

        Masks are applied at placement time, outside a node's own content
        cache.  Two distinct reader shapes follow:

        * An **isolated** reader applies its mask when it is placed into its
          parent, so its self-contained content cache stays valid; its
          placement boundary (which bakes the mask) is redone only on the
          edited rect (a ``mask-apply`` event).
        * A **pass-through** reader has no own cache - its mask is baked into
          the nearest enclosing **isolated ancestor's** local render.  That
          ancestor's cache must therefore be invalidated over the rect so the
          pass-through mask is recomputed inside it.
        """
        if mask_id not in self._mask_versions:
            raise KeyError("unknown mask %r" % mask_id)
        self._mask_versions[mask_id] += 1
        affected = []
        for node in self.project.nodes_using_mask(mask_id):
            if self._effectively_isolated(node):
                # Content cache survives; only the mask-bearing placement
                # boundary must be redone over the edited rect.
                affected.append({
                    "uid": node.uid, "name": node.name,
                    "boundary": "isolated-placement", "rect": rect,
                })
                continue
            ancestor = node.parent
            while ancestor is not None and not self._effectively_isolated(ancestor):
                ancestor = ancestor.parent
            if ancestor is not None:
                self._mark_dirty(ancestor.uid, rect)
                affected.append({
                    "uid": ancestor.uid, "name": ancestor.name,
                    "boundary": "enclosing-isolated-content", "rect": rect,
                    "via": node.name,
                })
        self._record_edit({
            "seq": self._edit_seq, "type": "mask", "mask_id": mask_id,
            "rect": rect, "affected": affected,
        })

    def invalidate_all(self):
        self._cache.clear()
        self._alpha_cache.clear()
        self.log.clear()

    def sync_project_nodes(self):
        """Register nodes added/removed by structural tree edits, keeping
        version counters for nodes that already existed."""
        current = {n.uid: n for n in self.project.all_nodes()}
        for uid in list(self._node_versions):
            if uid not in current:
                self._node_versions.pop(uid, None)
                self._cache.pop(uid, None)
                self._alpha_cache.pop(uid, None)
        for uid in current:
            self._node_versions.setdefault(uid, 0)
        for mid in self.project.masks:
            self._mask_versions.setdefault(mid, 0)
        # Structural edits change parent/backdrop identity, so a previous
        # baseline cannot justify frozen splices until a fresh full run.
        self._last_run_node_versions = dict(self._node_versions)
        self._last_run_mask_versions = dict(self._mask_versions)

    def _record_edit(self, edit):
        self._edit_seq += 1
        edit["seq"] = self._edit_seq
        self.edits.append(edit)
        if len(self.edits) > 50:
            self.edits = self.edits[-50:]

    def _mark_dirty(self, uid, rect):
        entry = self._cache.get(uid)
        if entry is not None:
            entry.dirty_rects.append(rect)

    # ----------------------------------------------------------- properties

    def _effectively_isolated(self, node):
        if not isinstance(node, Group):
            return True
        if node.isolated:
            return True
        if node.opacity < 1.0 or node.blend_mode != "normal":
            raise LayerPaintError(
                "pass-through group %r requires opacity 1 and normal blend"
                % node.name)
        return False

    def _cache_key(self, node):
        # Local caches hold pure content only: mask/opacity/blend/clipping are
        # placement-stage concerns, so mask versions never enter this key.
        nv = self._node_versions[node.uid]
        if isinstance(node, Group) and self._effectively_isolated(node):
            subtree = tuple(self._node_versions[n.uid]
                            for n in node.walk() if n is not node)
            return ("group", nv, subtree)
        return ("node", nv)

    def _mask_version_of(self, node):
        if node.mask_id is None:
            return None
        return self._mask_versions.get(node.mask_id, 0)

    # ---------------------------------------------------------------- evaluate

    def evaluate(self):
        """Full evaluation on a transparent canvas; returns (raster, trace)."""
        self.project.validate()
        self._begin_run(frozen=False)
        trace = []
        raster = self.backend.empty_raster(self.project.width, self.project.height)
        raster = self._render_children(self.project.root, raster, FULL, trace)
        if self.project.root.mask_id is not None:
            raster = self._apply_node_mask(self.project.root, raster, FULL)
        trace.append(self._trace_entry(self.project.root, raster))
        self._root_raster = raster
        self._end_run(trace)
        return raster, trace

    def reevaluate_dirty_rect(self, rect):
        """Recompute one rectangle after a tracked edit.

        The rectangle is rendered from a transparent canvas through the whole
        tree (so ordering, clipping bases and pass-through backdrops are
        derived exactly as in a full run), then spliced into the previous
        root raster.  Isolated subtrees untouched by the tracked edits reuse
        their previous composite boundary via a frozen splice, so an edit in
        one branch never re-runs a deep unchanged neighbor branch.
        """
        self.project.validate()
        self._begin_run(frozen=True, rect=rect)
        trace = []
        fresh = self.backend.empty_raster(self.project.width, self.project.height)
        patched = self._render_children(self.project.root, fresh, rect, trace)
        if self.project.root.mask_id is not None:
            patched = self._apply_node_mask(self.project.root, patched, rect)
        x0, y0, x1, y1 = rect
        if self._root_raster is None:
            self._root_raster = patched
        else:
            for y in range(y0, y1):
                length = (x1 - x0) * 4
                src = (y * self.project.width + x0) * 4
                self._root_raster.data[src:src + length] = \
                    patched.data[src:src + length]
        trace.append(self._trace_entry(self.project.root, self._root_raster))
        self._end_run()
        return self._root_raster, trace

    def evaluate_rect(self, rect):
        return self.reevaluate_dirty_rect(rect)

    def pixel_trace(self, x, y):
        """Per-stage straight RGBA for one pixel, bottom layer to final."""
        raster, trace = self.evaluate()
        i = (y * self.project.width + x) * 4
        stages = []
        for stage in trace:
            r = stage["raster"]
            stages.append({
                "uid": stage["uid"],
                "name": stage["name"],
                "kind": stage["kind"],
                "blend_mode": stage["blend_mode"],
                "opacity": stage["opacity"],
                "isolated": stage["isolated"],
                "clipped": stage["clipped"],
                "rgba_bytes": tuple(r.data[i:i + 4]),
                "rgba_unit": tuple(round(v / 255.0, 6) for v in r.data[i:i + 4]),
            })
        return stages

    # ------------------------------------------------------------- rendering

    def _render_children(self, group, backdrop, rect, trace):
        """Composite ``group``'s children onto ``backdrop`` (mutated/cloned)."""
        current = backdrop
        # A clipping base records both its own alpha raster and the backdrop
        # that existed *before* the base was placed; the lerp for later
        # clipped siblings interpolates against that pre-base backdrop.
        base_alpha = None
        base_backdrop = None
        for node in group.children:
            clipped = node.clipped
            # Lazy snapshot: only a clipping base needs a pre-placement
            # backdrop clone.  Ordinary compositing never reads it, so deep
            # chains of non-clipped nodes allocate no per-node buffer here.
            if clipped or self._has_later_clipped_sibling(group, node):
                before = current.clone()
            else:
                before = None
                self.stats["clone-skipped"] += 1
            if isinstance(node, Layer) or self._effectively_isolated(node):
                transparent_backdrop = self._rect_is_transparent(current, rect)
                current = self._place_or_freeze(
                    node, current, before, base_alpha, base_backdrop,
                    rect, trace, clipped, transparent_backdrop)
            else:
                current = self._render_passthrough(node, current, before,
                                                   base_alpha, base_backdrop,
                                                   rect, trace)
            if not clipped:
                base_alpha = self._node_alpha_raster(node, rect)
                base_backdrop = before if before is not None else current
        return current

    def _has_later_clipped_sibling(self, group, node):
        seen = False
        for sibling in group.children:
            if sibling is node:
                seen = True
                continue
            if seen and sibling.clipped:
                return True
        return False

    def _rect_is_transparent(self, raster, rect):
        """True if every alpha byte inside ``rect`` is zero.

        A frozen splice is only valid over an empty backdrop: on transparency
        the node's placed output equals its own render, so the previous placed
        rectangle can be copied verbatim.  Over a painted backdrop the prior
        output already contains blended color that cannot be unmixed, so such
        a node must be recomputed.
        """
        if rect is FULL:
            return not any(raster.data[3::4])
        x0, y0, x1, y1 = rect
        data, w = raster.data, raster.width
        for y in range(y0, y1):
            row = (y * w + x0) * 4 + 3
            for i in range(row, row + (x1 - x0) * 4, 4):
                if data[i] != 0:
                    return False
        return True

    def _node_alpha_raster(self, node, rect):
        """The node's own straight RGBA raster after its mask, before it is
        blended into any backdrop.  Its alpha channel is the clipping mask.

        Memoized per run: one base can serve a whole clipping chain, and the
        same base is also requested when a later non-clipped sibling resets
        the chain, so rendering it twice would be pure duplicate work.
        """
        cache_key = (
            self._node_versions[node.uid],
            self._mask_version_of(node),
            rect,
        )
        cached = self._alpha_cache.get(node.uid)
        if cached is not None and cached[0] == cache_key:
            self.stats["alpha-hit"] += 1
            self.log.append(("alpha-hit", node.uid, node.name, rect))
            return cached[1]
        if isinstance(node, Layer) or self._effectively_isolated(node):
            raster = self._local_render(node, rect)
        else:
            # Pass-through group's own footprint: its children rendered onto a
            # transparent canvas, ignoring the outer backdrop.
            raster = self.backend.empty_raster(self.project.width, self.project.height)
            raster = self._render_children(node, raster, rect, [])
        raster = self._apply_node_mask(node, raster, rect)
        self._alpha_cache[node.uid] = (cache_key, raster)
        return raster

    def _place_or_freeze(self, node, backdrop, before, base_alpha,
                         base_backdrop, rect, trace, clipped,
                         transparent_backdrop):
        """Place an isolated node, reusing its previous boundary when frozen."""
        if self._can_freeze(node, clipped, rect, transparent_backdrop):
            return self._frozen_splice(node, backdrop, rect, trace)
        local = self._local_render(node, rect)
        return self._place(node, local, backdrop, before, base_alpha,
                          base_backdrop, rect, trace, clipped)

    def _can_freeze(self, node, clipped, rect, transparent_backdrop):
        """Whether the node's previous placed result is still valid.

        Requirements, all derived from real dependencies:

        * a previous placed raster exists (there is something to splice);
        * this is a dirty-rect run, not a full evaluation;
        * the node is an unclipped, isolated boundary (a clipped node's result
          depends on the clipping base and pre-base backdrop, and a
          pass-through group has no own boundary);
        * the incoming backdrop rectangle is empty, so the placed output is
          just the node's own render and needs no un-blending;
        * the node is not on an edited node's ancestor chain (those carry the
          dirty rectangle);
        * no node in its subtree, and no mask read inside it, changed.
        """
        if not self._frozen_mode or rect is FULL:
            return False
        if self._last_placed.get(node.uid) is None:
            return False
        if clipped or not self._effectively_isolated(node):
            return False
        if not transparent_backdrop:
            return False
        # A group on an edited node's ancestor chain carries the dirty
        # rectangle: freezing it would keep stale edge pixels (soft strokes
        # antialias just outside the declared rect).  Only true sibling
        # branches - never an ancestor of the edit - may be frozen.
        if node.uid in self._edited_ancestor_uids:
            return False
        for sub in node.walk():
            if self._node_versions[sub.uid] != \
                    self._last_run_node_versions.get(sub.uid, -1):
                return False
            # Any mask read anywhere in the subtree must be unchanged too:
            # a pass-through descendant bakes its mask into this boundary.
            if sub.mask_id is not None and (
                self._mask_versions[sub.mask_id]
                != self._last_run_mask_versions.get(sub.mask_id, -1)
            ):
                return False
        return True

    def _frozen_splice(self, node, backdrop, rect, trace):
        """Copy the node's previous placed rectangle onto ``backdrop``.

        The source is the node's own output from the previous run - its local
        render already placed over the backdrop *as visible at that node* -
        never the root raster (which would include later siblings).  Inside a
        fresh transparent re-evaluation the backdrop below this frozen,
        unclipped isolated boundary is empty in the same way, so splicing its
        prior placed rectangle reproduces exactly what a re-run would produce.
        """
        previous = self._last_placed.get(node.uid)
        x0, y0, x1, y1 = rect
        out = backdrop
        for y in range(y0, y1):
            length = (x1 - x0) * 4
            s = (y * previous.width + x0) * 4
            d = (y * out.width + x0) * 4
            out.data[d:d + length] = previous.data[s:s + length]
        self.stats["frozen-splice"] += 1
        self.log.append(("frozen-splice", node.uid, node.name, rect))
        trace.append(self._trace_entry(node, out))
        return out

    def _place(self, node, local, backdrop, before, base_alpha, base_backdrop,
               rect, trace, clipped=None):
        """Blend a self-contained local render into the parent backdrop."""
        if clipped is None:
            clipped = node.clipped
        if clipped:
            # Clipping replaces normal placement: lerp the node's own render
            # against the pre-base backdrop using the base layer's own alpha.
            if base_alpha is None:
                raise LayerPaintError(
                    "clipped node %r has no clipping base" % node.name)
            out = self.backend.clip_lerp(base_backdrop, local, base_alpha, rect)
        else:
            out = self.backend.composite(
                backdrop, local, node.blend_mode, node.opacity, rect)
        out = self._apply_node_mask(node, out, rect)
        self.stats["place"] += 1
        self.log.append(("place", node.uid, node.name, rect))
        trace.append(self._trace_entry(node, out))
        return out

    def _render_passthrough(self, group, backdrop, before, base_alpha,
                            base_backdrop, rect, trace):
        """Pass-through group: children read the live parent backdrop."""
        current = self._render_children(group, backdrop, rect, trace)
        if group.clipped:
            if base_alpha is None:
                raise LayerPaintError(
                    "clipped group %r has no clipping base" % group.name)
            current = self.backend.clip_lerp(
                base_backdrop, current, base_alpha, rect)
        current = self._apply_node_mask(group, current, rect)
        self.stats["passthrough"] += 1
        self.log.append(("passthrough", group.uid, group.name, rect))
        trace.append(self._trace_entry(group, current))
        return current

    def _apply_node_mask(self, node, raster, rect):
        if node.mask_id is None:
            return raster
        out = self.backend.apply_mask(
            raster, self.project.masks[node.mask_id], rect)
        self.log.append(("mask-apply", node.uid, node.name, rect,
                         node.mask_id))
        return out

    # --------------------------------------------------------- local renders

    def _local_render(self, node, rect):
        """Cached self-contained render for layers and isolated groups.

        The cached raster never includes the node's own mask, clipping,
        opacity or blend mode - those are parent-placement concerns - so the
        cache is keyed purely on content and child versions.
        """
        key = self._cache_key(node)
        entry = self._cache.get(node.uid)
        if entry is not None and entry.key == key and not entry.dirty_rects:
            self.stats["cache-hit"] += 1
            self.log.append(("cache-hit", node.uid, node.name, rect))
            return entry.raster
        if entry is None:
            raster = self._build_local(node, FULL)
            self.stats["rebuild"] += 1
            self.log.append(("rebuild", node.uid, node.name, FULL))
            entry = CacheEntry(raster, key)
            self._cache[node.uid] = entry
            return raster
        for d in entry.dirty_rects or [FULL]:
            if d is FULL or self._covers_canvas(d):
                entry.raster = self._build_local(node, FULL)
                self.stats["rebuild"] += 1
                self.log.append(("rebuild", node.uid, node.name, FULL))
                break
            entry.raster = self._patch_local(node, entry.raster, d)
            self.stats["recompute-rect"] += 1
            self.log.append(("recompute-rect", node.uid, node.name, d))
        entry.dirty_rects = []
        entry.key = key
        return entry.raster

    def _covers_canvas(self, rect):
        if rect is FULL:
            return True
        x0, y0, x1, y1 = rect
        return (x0 == 0 and y0 == 0
                and x1 == self.project.width and y1 == self.project.height)

    def _build_local(self, node, rect):
        if isinstance(node, Layer):
            return node.raster.clone()
        raster = self.backend.empty_raster(self.project.width, self.project.height)
        return self._render_children(node, raster, rect, [])

    def _patch_local(self, node, previous, rect):
        """Recompute only ``rect`` of a cached local render.

        The group is always rebuilt on a transparent canvas (re-running
        children on the old raster would composite it a second time); only the
        rebuilt rectangle is spliced back into the previous cache.
        """
        x0, y0, x1, y1 = rect
        if isinstance(node, Group):
            fresh = self.backend.empty_raster(
                self.project.width, self.project.height)
            rebuilt = self._render_children(node, fresh, rect, [])
            out = previous
            for y in range(y0, y1):
                length = (x1 - x0) * 4
                src = (y * out.width + x0) * 4
                out.data[src:src + length] = rebuilt.data[src:src + length]
            return out
        patch = self._build_local(node, rect)
        out = previous
        for y in range(y0, y1):
            src = (y * node.raster.width + x0) * 4
            dst = (y * out.width + x0) * 4
            out.data[dst:dst + (x1 - x0) * 4] = \
                patch.data[src:src + (x1 - x0) * 4]
        return out

    # -------------------------------------------------- "why redraw" lookup

    def why_redraw(self, uid, rect=None):
        """Reverse lookup: why must a node's cached region be redrawn?

        Returns the dependency chain from the most recent tracked edits to the
        requested node, distinguishing direct content edits, ancestor
        propagation, mask placement invalidation (isolated readers) and
        pass-through mask baking into an enclosing isolated ancestor.
        """
        target = self.project.get(uid)
        ancestors = []
        cur = target.parent
        while cur is not None:
            ancestors.append(cur)
            cur = cur.parent
        ancestor_ids = {a.uid for a in ancestors}
        reasons = []
        for edit in self.edits:
            if edit["type"] == "node":
                if edit["uid"] == uid:
                    reasons.append({
                        "cause": "direct-content-edit",
                        "source": edit["name"], "rect": edit["rect"],
                        "path": [edit["name"]],
                    })
                elif edit["uid"] in ancestor_ids:
                    chain = [self.project.get(edit["uid"]).name, target.name]
                    reasons.append({
                        "cause": "ancestor-content-edit",
                        "source": edit["name"], "rect": edit["rect"],
                        "path": chain,
                    })
            else:
                reasons.extend(
                    self._mask_hits_for(edit, target, ancestor_ids))
        return {
            "uid": uid, "name": target.name,
            "mask_id": target.mask_id, "rect": rect,
            "reasons": reasons,
        }

    def _mask_hits_for(self, edit, target, ancestor_ids):
        mask_id = edit["mask_id"]
        out = []
        for node in self.project.nodes_using_mask(mask_id):
            if node.uid == target.uid and self._effectively_isolated(node):
                out.append({
                    "cause": "mask-placement-edit",
                    "source": "mask:%s" % mask_id, "rect": edit["rect"],
                    "path": ["mask:%s" % mask_id, node.name],
                    "boundary": "isolated-placement",
                })
            elif not self._effectively_isolated(node):
                ancestor = node.parent
                chain = ["mask:%s" % mask_id, node.name]
                while ancestor is not None and \
                        not self._effectively_isolated(ancestor):
                    chain.append(ancestor.name)
                    ancestor = ancestor.parent
                if ancestor is not None:
                    chain.append(ancestor.name)
                    if ancestor.uid == target.uid or \
                            ancestor.uid in ancestor_ids:
                        out.append({
                            "cause": "mask-baked-into-isolated-ancestor",
                            "source": "mask:%s" % mask_id,
                            "rect": edit["rect"], "path": chain,
                            "boundary": "enclosing-isolated-content",
                        })
        return out

    def mask_propagation(self, mask_id, rect=None):
        """Where an edit to ``mask_id`` propagates, per reader cache boundary."""
        if mask_id not in self.project.masks:
            raise KeyError("unknown mask %r" % mask_id)
        readers = []
        for node in self.project.nodes_using_mask(mask_id):
            if self._effectively_isolated(node):
                readers.append({
                    "uid": node.uid, "name": node.name,
                    "boundary": "isolated-placement",
                    "content_cache": "reused",
                    "action": "re-apply mask on edited rect",
                    "rect": rect,
                })
                continue
            ancestor = node.parent
            chain = [node.name]
            while ancestor is not None and not self._effectively_isolated(ancestor):
                chain.append(ancestor.name)
                ancestor = ancestor.parent
            readers.append({
                "uid": node.uid, "name": node.name,
                "boundary": "passthrough-baked",
                "enclosing_isolated": ancestor.name if ancestor else None,
                "content_cache": ("invalidated rect %r" % (rect,)
                                  if ancestor else "no isolated ancestor"),
                "path": chain, "rect": rect,
            })
        return {"mask_id": mask_id, "rect": rect, "readers": readers}

    # ------------------------------------------------------------- bookkeeping

    def _trace_entry(self, node, raster):
        return {
            "uid": node.uid,
            "name": node.name,
            "kind": node.kind,
            "blend_mode": node.blend_mode,
            "opacity": node.opacity,
            "isolated": self._effectively_isolated(node)
            if isinstance(node, Group) else True,
            "clipped": node.clipped,
            "raster": raster,
        }

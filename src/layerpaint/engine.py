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
"""

from .backends.reference import ReferenceBackend
from .model import Group, Layer, LayerPaintError

FULL = None  # whole-canvas rect sentinel


class CacheEntry:
    def __init__(self, raster, key):
        self.raster = raster
        self.key = key
        self.dirty_rects = []


class Evaluator:
    def __init__(self, project, backend=None):
        self.project = project
        self.backend = backend or ReferenceBackend()
        self._cache = {}
        self._root_raster = None
        self._node_versions = {n.uid: 0 for n in project.all_nodes()}
        self._mask_versions = {mid: 0 for mid in project.masks}
        self.log = []

    # ------------------------------------------------------------- mutations

    def touch_node(self, uid, rect=FULL):
        """Record an edit to a node (raster/opacity/blend/tree position)."""
        if uid not in self._node_versions:
            raise KeyError("unknown node uid %r" % uid)
        self._node_versions[uid] += 1
        cur = self.project.get(uid)
        while cur is not None:
            self._mark_dirty(cur.uid, rect)
            cur = cur.parent

    def touch_mask(self, mask_id, rect=FULL):
        """Propagate a mask edit along the real read/write dependencies.

        Masks are applied at placement time, outside a node's own content
        cache.  Two distinct reader shapes follow:

        * An **isolated** reader applies its mask when it is placed into its
          parent, so its self-contained content cache stays valid and the mask
          is simply re-applied on the edited rect (a ``mask-apply`` event).
        * A **pass-through** reader has no own cache - its mask is baked into
          the nearest enclosing **isolated ancestor's** local render.  That
          ancestor's cache must therefore be invalidated over the rect so the
          pass-through mask is recomputed inside it.
        """
        if mask_id not in self._mask_versions:
            raise KeyError("unknown mask %r" % mask_id)
        self._mask_versions[mask_id] += 1
        for node in self.project.nodes_using_mask(mask_id):
            if self._effectively_isolated(node):
                continue
            ancestor = node.parent
            while ancestor is not None and not self._effectively_isolated(ancestor):
                ancestor = ancestor.parent
            if ancestor is not None:
                self._mark_dirty(ancestor.uid, rect)

    def invalidate_all(self):
        self._cache.clear()
        self.log.clear()

    def sync_project_nodes(self):
        """Register nodes added/removed by structural tree edits, keeping
        version counters for nodes that already existed."""
        current = {n.uid: n for n in self.project.all_nodes()}
        for uid in list(self._node_versions):
            if uid not in current:
                self._node_versions.pop(uid, None)
                self._cache.pop(uid, None)
        for uid in current:
            self._node_versions.setdefault(uid, 0)
        for mid in self.project.masks:
            self._mask_versions.setdefault(mid, 0)

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

    # ---------------------------------------------------------------- evaluate

    def evaluate(self):
        """Full evaluation on a transparent canvas; returns (raster, trace)."""
        self.project.validate()
        self.log = []
        trace = []
        raster = self.backend.empty_raster(self.project.width, self.project.height)
        raster = self._render_children(self.project.root, raster, FULL, trace)
        if self.project.root.mask_id is not None:
            raster = self._apply_node_mask(self.project.root, raster, FULL)
        trace.append(self._trace_entry(self.project.root, raster))
        self._root_raster = raster
        return raster, trace

    def reevaluate_dirty_rect(self, rect):
        """Recompute one rectangle after a tracked edit.

        The rectangle is rendered from a transparent canvas through the whole
        tree (so ordering, clipping bases and pass-through backdrops are
        derived exactly as in a full run), then spliced into the previous
        root raster.  Every mask-apply and backend op inside carries the rect,
        which is what makes the isolated/pass-through cache boundary visible
        in :attr:`log`.
        """
        self.project.validate()
        self.log = []
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
                self._root_raster.data[src:src + length] = patched.data[src:src + length]
        trace.append(self._trace_entry(self.project.root, self._root_raster))
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
            before = current.clone()
            if isinstance(node, Layer) or self._effectively_isolated(node):
                local = self._local_render(node, rect)
                current = self._place(node, local, current, before,
                                      base_alpha, base_backdrop, rect, trace)
            else:
                current = self._render_passthrough(node, current, before,
                                                   base_alpha, base_backdrop,
                                                   rect, trace)
            if not node.clipped:
                base_alpha = self._node_alpha_raster(node, rect)
                base_backdrop = before
        return current

    def _node_alpha_raster(self, node, rect):
        """The node's own straight RGBA raster after its mask, before it is
        blended into any backdrop.  Its alpha channel is the clipping mask."""
        if isinstance(node, Layer) or self._effectively_isolated(node):
            raster = self._local_render(node, rect)
        else:
            # Pass-through group's own footprint: its children rendered onto a
            # transparent canvas, ignoring the outer backdrop.
            raster = self.backend.empty_raster(self.project.width, self.project.height)
            raster = self._render_children(node, raster, rect, [])
        return self._apply_node_mask(node, raster, rect)

    def _place(self, node, local, backdrop, before, base_alpha, base_backdrop,
               rect, trace):
        """Blend a self-contained local render into the parent backdrop."""
        if node.clipped:
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
            self.log.append(("cache-hit", node.uid, node.name, rect))
            return entry.raster
        if entry is None:
            raster = self._build_local(node, FULL)
            self.log.append(("rebuild", node.uid, node.name, FULL))
            entry = CacheEntry(raster, key)
            self._cache[node.uid] = entry
            return raster
        for d in entry.dirty_rects or [FULL]:
            if d is FULL or self._covers_canvas(d):
                entry.raster = self._build_local(node, FULL)
                self.log.append(("rebuild", node.uid, node.name, FULL))
                break
            entry.raster = self._patch_local(node, entry.raster, d)
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
            raster = node.raster.clone()
            return raster
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
            out.data[dst:dst + (x1 - x0) * 4] = patch.data[src:src + (x1 - x0) * 4]
        return out

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

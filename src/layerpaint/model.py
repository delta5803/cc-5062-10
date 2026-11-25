"""Layer tree model.

Two node kinds:

* :class:`Layer`  - a raster plus opacity, blend mode, mask, clipping flag.
* :class:`Group`  - ordered children (bottom first), opacity, blend mode,
  an ``isolated`` flag (False means pass-through), mask and clipping flag.

Groups and layers intentionally share the same compositing-relevant fields so
the evaluator can treat them uniformly.  Masks are Raster objects referenced by
id from the project, which lets an isolated group and a pass-through group
share one mask and lets the cache reason about that dependency.
"""

from .blend import BLEND_MODES


class Node:
    def __init__(self, name, opacity=1.0, blend_mode="normal",
                 isolated=True, clipped=False, mask_id=None):
        self.name = name
        self.opacity = float(opacity)
        if blend_mode not in BLEND_MODES:
            raise ValueError("unknown blend mode: %r" % blend_mode)
        self.blend_mode = blend_mode
        self.isolated = bool(isolated)
        self.clipped = bool(clipped)
        self.mask_id = mask_id
        self.parent = None
        self.uid = None  # assigned by Project

    def walk(self):
        yield self
        if isinstance(self, Group):
            for child in self.children:
                yield from child.walk()


class Layer(Node):
    kind = "layer"

    def __init__(self, name, raster, **kw):
        super().__init__(name, **kw)
        self.raster = raster


class Group(Node):
    kind = "group"

    def __init__(self, name, children=None, **kw):
        super().__init__(name, **kw)
        self.children = []
        for child in children or ():
            self.add(child)

    def add(self, node):
        node.parent = self
        self.children.append(node)
        return node

    def is_empty(self):
        for child in self.children:
            if isinstance(child, Group):
                if not child.is_empty():
                    return False
            else:
                return False
        return True


class Project:
    """Root container: canvas size, node tree, shared mask registry."""

    def __init__(self, width, height, root):
        self.width = int(width)
        self.height = int(height)
        self.root = root
        self.masks = {}          # mask_id -> Raster (grayscale alpha)
        self._next_uid = 0
        self._assign_uids(root)

    def _assign_uids(self, node):
        node.uid = self._next_uid
        self._next_uid += 1
        if isinstance(node, Group):
            for child in node.children:
                self._assign_uids(child)

    def all_nodes(self):
        return list(self.root.walk())

    def get(self, uid):
        for node in self.all_nodes():
            if node.uid == uid:
                return node
        raise KeyError(uid)

    def add_mask(self, mask_id, raster):
        self.masks[mask_id] = raster
        return mask_id

    def nodes_using_mask(self, mask_id):
        return [n for n in self.all_nodes() if n.mask_id == mask_id]

    def validate(self):
        """Raise :class:`LayerPaintError` on any structurally invalid tree."""
        for node in self.all_nodes():
            if not 0.0 <= node.opacity <= 1.0:
                raise LayerPaintError(
                    "opacity out of range on %r: %r" % (node.name, node.opacity))
            if node.mask_id is not None and node.mask_id not in self.masks:
                raise LayerPaintError(
                    "node %r references missing mask %r"
                    % (node.name, node.mask_id))
            if isinstance(node, Layer) and (
                node.raster.width != self.width
                or node.raster.height != self.height
            ):
                raise LayerPaintError(
                    "layer %r raster size does not match canvas" % node.name)
            if isinstance(node, Group) and not node.isolated:
                if node is self.root:
                    raise LayerPaintError("the root group cannot be pass-through")
                if node.opacity < 1.0 or node.blend_mode != "normal":
                    raise LayerPaintError(
                        "pass-through group %r requires opacity 1 and normal "
                        "blend mode (got opacity=%r blend=%r)"
                        % (node.name, node.opacity, node.blend_mode))
        self._check_clipping_chains()
        return True

    def _check_clipping_chains(self):
        def recurse(group):
            for idx, node in enumerate(group.children):
                if node.clipped and idx == 0:
                    raise LayerPaintError(
                        "node %r is clipped but has no underlying base layer"
                        % node.name)
                if isinstance(node, Group):
                    recurse(node)
        recurse(self.root)


class LayerPaintError(Exception):
    pass

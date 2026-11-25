"""layerpaint: digital painting layer compositing core."""

from .backends.accelerated import AcceleratedBackend
from .backends.reference import ReferenceBackend
from .demo import build_demo_project
from .engine import Evaluator
from .model import Group, Layer, Project
from .raster import Raster, read_png, write_png
from .serialization import from_dict, load, save, to_dict

__all__ = [
    "AcceleratedBackend",
    "ReferenceBackend",
    "Evaluator",
    "Group",
    "Layer",
    "Project",
    "Raster",
    "read_png",
    "write_png",
    "load",
    "save",
    "to_dict",
    "from_dict",
    "build_demo_project",
]

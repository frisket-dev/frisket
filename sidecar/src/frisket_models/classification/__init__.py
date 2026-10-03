"""CPU-native local text classifiers with an import-light public surface."""

from .gliclass_base import GLiClassBase
from .jeff import Jeff

__all__ = ["GLiClassBase", "Jeff"]

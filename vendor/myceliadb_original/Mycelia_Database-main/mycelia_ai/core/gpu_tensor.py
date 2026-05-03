"""Utility helpers for managing GPU-backed tensors through the custom driver."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Tuple


@dataclass
class GPUTensor:
    """Lightweight descriptor representing a GPU buffer.

    The tensor only stores a handle received from the driver and the logical
    shape/dtype information that is meaningful for the Python layer.  Actual
    memory management is delegated to the driver which allows zero-copy
    interoperability across kernels.
    """

    handle: int
    shape: Tuple[int, ...]
    dtype: str
    payload: Any | None = None

    def as_kernel_args(self) -> Iterable[Any]:
        """Return a representation that can be passed to driver kernels."""

        return (self.handle, *self.shape)


class TensorArena:
    """Book-keeping helper that manages a collection of GPU tensors."""

    def __init__(self) -> None:
        self._tensors: dict[str, GPUTensor] = {}

    def register(self, name: str, tensor: GPUTensor) -> None:
        if name in self._tensors:
            raise KeyError(f"Tensor '{name}' already registered")
        self._tensors[name] = tensor

    def get(self, name: str) -> GPUTensor:
        return self._tensors[name]

    def release(self, name: str) -> None:
        self._tensors.pop(name, None)

    def clear(self) -> None:
        self._tensors.clear()

    def __contains__(self, name: str) -> bool:  # pragma: no cover - trivial
        return name in self._tensors

    def __iter__(self):  # pragma: no cover - convenience proxy
        return iter(self._tensors.items())

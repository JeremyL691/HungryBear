# hungrybear/adapters/__init__.py

from __future__ import annotations

from typing import Dict, Type

from ..config import CampusConfig
from ..http import Fetcher
from .base import Adapter, AdapterError
from .berkeley import BerkeleyAdapter

ADAPTERS: Dict[str, Type[Adapter]] = {
    "berkeley": BerkeleyAdapter,
}


def build_adapter(config: CampusConfig, fetcher: Fetcher) -> Adapter:
    try:
        cls = ADAPTERS[config.adapter]
    except KeyError:
        raise KeyError(f"campus {config.id!r}: unknown adapter {config.adapter!r}") from None
    return cls(config, fetcher)


__all__ = ["ADAPTERS", "Adapter", "AdapterError", "build_adapter"]

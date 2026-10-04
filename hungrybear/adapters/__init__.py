# hungrybear/adapters/__init__.py

from __future__ import annotations

from datetime import date
from typing import Dict, Optional, Type

from ..config import CampusConfig
from ..http import Fetcher
from .base import Adapter, AdapterError
from .berkeley import BerkeleyAdapter
from .foodpro import FoodProAdapter
from .ucla import UclaAdapter
from .ucsb import UcsbAdapter
from .ucsd import UcsdAdapter

ADAPTERS: Dict[str, Type[Adapter]] = {
    "berkeley": BerkeleyAdapter,
    "foodpro": FoodProAdapter,
    "ucla": UclaAdapter,
    "ucsb": UcsbAdapter,
    "ucsd": UcsdAdapter,
}


def build_adapter(config: CampusConfig, fetcher: Fetcher, today: Optional[date] = None) -> Adapter:
    try:
        cls = ADAPTERS[config.adapter]
    except KeyError:
        raise KeyError(f"campus {config.id!r}: unknown adapter {config.adapter!r}") from None
    return cls(config, fetcher, today)


__all__ = ["ADAPTERS", "Adapter", "AdapterError", "build_adapter"]

from dataclasses import dataclass, field
from typing import Callable


class SourceContractError(Exception):
    pass


@dataclass
class SourceSnapshot:
    source: str
    listings: list = field(default_factory=list)
    events: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)


@dataclass(frozen=True)
class SourceSpec:
    name: str
    cadence: str
    fetch: Callable[[], SourceSnapshot]
    baseline: bool = True

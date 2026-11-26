"""Stable private player directories, independent of the execution backend."""

from dataclasses import dataclass
from pathlib import Path

from civ_agent.civilization_paths import civilization_directory_name
from civ_agent.tls import private_directory


@dataclass(frozen=True)
class PlayerPaths:
    root: Path
    index: int
    civilization: str

    @property
    def player(self) -> str:
        return f"p{self.index}"

    @property
    def name(self) -> str:
        return civilization_directory_name(self.civilization)

    @property
    def coordinator(self) -> Path:
        return self.root / "coordinator" / self.name

    @property
    def store(self) -> Path:
        return self.root / "player-stores" / self.name

    @property
    def service(self) -> Path:
        return self.root / "agent-services" / self.name

    @property
    def security(self) -> Path:
        return self.service / "security"

    @property
    def coordinator_security(self) -> Path:
        return self.service / "coordinator-security"

    @property
    def worker_config(self) -> Path:
        return self.service / "worker.json"

    def prepare(self) -> None:
        for directory in (self.coordinator, self.store, self.security, self.coordinator_security):
            private_directory(directory)


# Retained operator records use this name; it has no worker or scheduler behavior.
ManagedPlayerPaths = PlayerPaths

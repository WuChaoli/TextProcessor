from dataclasses import dataclass
from typing import Literal

StartupStage = Literal[
    "starting",
    "validating_release",
    "loading_tokenizer",
    "loading_top_triple_classifier",
    "loading_end_doc_classifier",
    "smoke_testing",
    "ready",
    "failed",
    "stopping",
]


@dataclass
class HealthState:
    stage: StartupStage = "starting"
    ready: bool = False

    def transition(self, stage: StartupStage) -> None:
        self.stage = stage
        self.ready = stage == "ready"

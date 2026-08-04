from classification_service.infrastructure.model.setfit_loader import (
    SetFitClassifierAdapter,
    SetFitModel,
)


class TopTripleClassifier(SetFitClassifierAdapter):
    """SetFit adapter for the release's ordered 18 top-triple labels."""

    def __init__(self, model: SetFitModel, labels: tuple[str, ...]) -> None:
        super().__init__(model, labels, expected_label_count=18)

from classification_service.infrastructure.model.setfit_loader import (
    SetFitClassifierAdapter,
    SetFitModel,
)


class EndDocClassifier(SetFitClassifierAdapter):
    """SetFit adapter for the release's ordered six document labels."""

    def __init__(self, model: SetFitModel, labels: tuple[str, ...]) -> None:
        super().__init__(model, labels, expected_label_count=6)

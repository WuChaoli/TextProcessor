from classification_service.presentation.health import HealthState


def test_ready_only_at_ready_stage() -> None:
    state = HealthState()
    for stage in (
        "validating_release",
        "loading_tokenizer",
        "loading_top_triple_classifier",
        "loading_end_doc_classifier",
        "smoke_testing",
    ):
        state.transition(stage)  # type: ignore[arg-type]
        assert state.ready is False
    state.transition("ready")
    assert state.ready is True
    state.transition("stopping")
    assert state.ready is False

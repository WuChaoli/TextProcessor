from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_compose_has_one_internal_datajuicer_service() -> None:
    compose = (ROOT / "compose.yml").read_text(encoding="utf-8")

    assert "  datajuicer:" in compose
    assert "  datajuicer-worker:" not in compose
    assert "  datajuicer-beat:" not in compose
    block = compose.split("  datajuicer:", 1)[1].split("\n  ", 1)[0]
    assert "traefik" not in block.lower()
    assert "ports:" not in block


def test_container_runs_three_process_supervisor_as_non_root() -> None:
    dockerfile = (ROOT / "services" / "datajuicer_service" / "Dockerfile").read_text(encoding="utf-8")

    assert "USER datajuicer" in dockerfile
    assert 'CMD ["python", "-m", "datajuicer_service.process_manager"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile

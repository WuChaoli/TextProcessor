import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
EXPECTED = {"frontend", "backend-api", "task-runner", "docling", "classification", "datajuicer", "redis", "db"}


def test_default_compose_has_exactly_eight_services() -> None:
    base = yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))["services"]
    docling = yaml.safe_load((ROOT / "compose.docling.yml").read_text(encoding="utf-8"))["services"]
    defaults = {name for name, service in {**base, **docling}.items() if not service.get("profiles")}

    assert defaults == EXPECTED
    assert {"adminer", "prestart", "extraction-worker", "extraction-beat"}.isdisjoint(defaults)


def test_only_public_entry_services_join_traefik_network() -> None:
    services = yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))["services"]
    public = {name for name, service in services.items() if "traefik-public" in service.get("networks", [])}

    assert public == {"frontend", "backend-api"}


def test_routes_do_not_use_background_tasks_or_import_processors() -> None:
    route_files = list((ROOT / "backend" / "app").rglob("routes.py"))
    forbidden_parts = {"processors", "algorithm"}
    for route_file in route_files:
        tree = ast.parse(route_file.read_text(encoding="utf-8"), filename=str(route_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "BackgroundTasks" not in {alias.name for alias in node.names}, route_file
                assert forbidden_parts.isdisjoint((node.module or "").split(".")), route_file
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert forbidden_parts.isdisjoint(alias.name.split(".")), route_file

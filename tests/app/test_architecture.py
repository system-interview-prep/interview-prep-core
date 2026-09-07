import ast
from pathlib import Path

from src.main import MODULES, create_app


def test_module_names_are_unique() -> None:
    names = [module.name for module in MODULES]
    assert len(names) == len(set(names))


def test_health_route_is_registered() -> None:
    paths = set(create_app().openapi()["paths"])
    assert "/health" in paths
    assert "/api/v1/matching/match" in paths


def test_modules_do_not_reach_into_other_module_internals() -> None:
    modules_root = Path("src/modules")
    public_files = {"facade", "schemas", "events"}
    violations: list[str] = []

    for path in modules_root.rglob("*.py"):
        relative = path.relative_to(modules_root)
        if len(relative.parts) < 2:
            continue
        owner = relative.parts[0]
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            imported = ""
            if isinstance(node, ast.ImportFrom):
                imported = node.module or ""
            elif isinstance(node, ast.Import) and node.names:
                imported = node.names[0].name
            prefix = "src.modules."
            if not imported.startswith(prefix):
                continue
            parts = imported.removeprefix(prefix).split(".")
            target = parts[0]
            if target == owner or len(parts) == 1:
                continue
            if len(parts) > 1 and parts[1] not in public_files:
                violations.append(f"{path}: {imported}")

    assert violations == []

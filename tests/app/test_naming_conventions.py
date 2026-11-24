from pathlib import Path


def test_runtime_code_uses_canonical_domain_terms() -> None:
    forbidden = ("job_profile", "job-profile", "JOBPROFILE", "JP_UPLOAD", "s3_key", "s3Key")
    violations: list[str] = []
    for root in (Path("src"), Path("tests")):
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts or path.resolve() == Path(__file__).resolve():
                continue
            content = path.read_text(encoding="utf-8-sig")
            for term in forbidden:
                if term in content:
                    violations.append(f"{path}: {term}")
    assert violations == []


def test_module_directories_use_plural_snake_case() -> None:
    invalid = [
        path.name
        for path in Path("src/modules").iterdir()
        if path.is_dir()
        and not path.name.startswith("__")
        and ("-" in path.name or path.name != path.name.lower())
    ]
    assert invalid == []

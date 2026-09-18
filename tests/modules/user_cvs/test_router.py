import pytest


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/users/me/cvs", {}),
        ("post", "/users/me/cvs", {"files": {"file": ("cv.pdf", b"pdf", "application/pdf")}}),
        ("get", "/users/me/cvs/cv-1", {}),
        ("get", "/users/me/cvs/cv-1/events", {}),
        ("patch", "/users/me/cvs/cv-1/parsed-data", {"json": {}}),
        ("post", "/users/me/cvs/cv-1/review", {"json": {"approved": True}}),
        ("post", "/users/me/cvs/cv-1/reparse", {}),
        ("delete", "/users/me/cvs/cv-1", {}),
    ],
)
def test_cv_routes_require_authentication(client, method: str, path: str, kwargs: dict) -> None:
    assert getattr(client, method)(path, **kwargs).status_code == 401

from fastapi.responses import Response

from src.modules.user_cvs.controllers.cv_controller import content_disposition_attachment


def test_content_disposition_supports_unicode_cv_filenames() -> None:
    header = content_disposition_attachment("CV Nguyễn Văn A.pdf")

    assert header.startswith('attachment; filename="CV_Nguyen_Van_A.pdf";')
    assert "filename*=UTF-8''CV%20Nguy%E1%BB%85n%20V%C4%83n%20A.pdf" in header
    # Starlette must be able to encode the header without raising a Latin-1 error.
    response = Response(b"%PDF", headers={"Content-Disposition": header})
    assert response.headers["content-disposition"] == header


def test_content_disposition_removes_header_injection_and_path_parts() -> None:
    header = content_disposition_attachment("../CV.pdf\r\nX-Injected: yes")

    assert "\r" not in header
    assert "\n" not in header
    assert "/" not in header
    assert 'filename="CV.pdf__X-Injected_yes"' in header

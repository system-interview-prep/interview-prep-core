# Cấu trúc module Admin

Mỗi thư mục con là một nhóm chức năng. Luồng xử lý chuẩn là:

`router.py` → `controller.py` → service/use case → `repository.py`

- `router.py`: đăng ký và xuất router của nhóm.
- `controller.py`: khai báo endpoint, kiểm tra quyền và chuyển request sang service.
- `service.py` hoặc file service theo nghiệp vụ: xử lý quy tắc, tổng hợp và định dạng dữ liệu.
- `repository.py`: truy vấn và ghi dữ liệu; SQL không đặt trong controller/service.
- `schemas.py`: request DTO/Pydantic schema cho endpoint có body.
- `shared/`: tiện ích dùng chung giữa các nhóm.

## Nhóm chức năng

- `ai/`: endpoint quan sát AI, cấu hình TTS/STT, Langfuse; truy vấn AI call nằm trong `repository.py`, DTO trong `schemas.py`.
- `overview/`: tổng quan và thống kê luyện tập; truy vấn trong `repository.py`, tổng hợp trong `service.py` và `practice_stats.py`.
- `processing/`: CV parsing và matching runs; truy vấn tách theo `cv_repository.py` và `matching_repository.py`, xử lý dữ liệu trong `cv_parsing_runs.py` và `matching_runs.py`.
- `sessions/`: phiên phỏng vấn và access log; SQL trong `repository.py`, ánh xạ dữ liệu trong `service.py`, request DTO trong `schemas.py`.
- `system/`: cấu hình, health check và hoạt động người dùng; SQL trong `repository.py`, các file nghiệp vụ định dạng kết quả.
- `shared/`: audit và hằng số truy vấn chung.

Admin hiện đọc các bảng do module interviews, users, CV và matching sở hữu; module này không sở hữu bảng ORM riêng. Vì vậy không tạo `models.py` trùng lặp. Các trường hợp có request body dùng `schemas.py`; query parameters được khai báo trực tiếp trên endpoint bằng kiểu FastAPI/Python.

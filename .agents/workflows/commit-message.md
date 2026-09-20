---
description: Tạo Git commit message tiếng Việt từ các thay đổi đã stage theo Conventional Commits.
---

# Tạo Git Commit Message

Hãy tạo Git commit message dựa trên các thay đổi đã được stage.

## Quy trình bắt buộc

1. Chạy:

   git diff --cached

2. CHỈ phân tích kết quả của `git diff --cached`.

3. Bỏ qua hoàn toàn:
   - unstaged changes
   - untracked files
   - các file chưa được stage

4. Sử dụng Conventional Commits:

   <type>[optional scope]: <description>

## Ngôn ngữ

- `type`: tiếng Anh
- `scope`: tiếng Anh
- `description`: BẮT BUỘC bằng tiếng Việt
- Tuyệt đối không tạo description bằng tiếng Anh.

Ví dụ đúng:

feat(auth): thêm đăng nhập bằng Google
fix(api): sửa lỗi xác thực Google ID token
ci: bổ sung kiểm tra backend và frontend
test(job): bổ sung integration test cho job service
docs: cập nhật hướng dẫn thiết lập môi trường
chore(env): cập nhật cấu hình môi trường mẫu

Ví dụ sai:

feat(auth): add Google login
fix(api): handle expired token
ci: add backend checks

## Type được phép

- feat
- fix
- docs
- style
- refactor
- perf
- test
- build
- ci
- chore

## Yêu cầu

- Nội dung phải ngắn gọn và cụ thể.
- Không dùng emoji.
- Không kết thúc description bằng dấu chấm.
- Không tự stage file.
- Không tự chạy `git commit`.
- Không tự push.
- Chỉ trả về commit message đề xuất.
---
description: Auto Commit Từng File Sau Khi Hoàn Thành Task
---

Sau khi hoàn thành toàn bộ task được giao, hãy tự động commit các file đã thay đổi theo quy trình dưới đây.

Mục tiêu
Mỗi file thay đổi tạo thành một Git commit riêng.
Không gom nhiều file vào cùng một commit.
Commit message phải theo Conventional Commits.
Description phải bằng tiếng Việt.
Chỉ commit những file do task hiện tại thay đổi.
Push một lần sau khi toàn bộ commit thành công.
Quy trình bắt buộc
1. Kiểm tra trạng thái Git

Chạy:

git status --short

Xác định tất cả file:

modified
added
deleted
renamed

Không xử lý những file không liên quan đến task hiện tại.

2. Xử lý từng file riêng biệt

Với MỖI file thay đổi, thực hiện lần lượt:

Stage duy nhất file đó:

git add -- "<file>"

Kiểm tra chính xác nội dung đã stage:

git diff --cached -- "<file>"

Phân tích CHỈ diff đã stage của file đó.
Sinh commit message theo Conventional Commits:

<type>[optional scope]: <description>

Trong đó:

type: tiếng Anh
scope: tiếng Anh nếu cần
description: bắt buộc tiếng Việt
Không dùng emoji
Không kết thúc bằng dấu chấm
Nội dung ngắn gọn nhưng mô tả đúng thay đổi

Type chỉ được dùng:

feat
fix
docs
style
refactor
perf
test
build
ci
chore

Ví dụ:

feat(auth): thêm giao diện đăng nhập quản trị viên

fix(job): sửa lỗi hiển thị mức độ phù hợp

refactor(profile): tách bộ chọn ngôn ngữ khỏi menu tài khoản

style(header): điều chỉnh khoảng cách khu vực hồ sơ

Commit file:

git commit -m "<commit-message>"

Kiểm tra commit thành công trước khi chuyển sang file tiếp theo.
3. Lặp lại

Tiếp tục quy trình trên cho đến khi tất cả file liên quan đến task đã được commit.

Tuyệt đối không chạy:

git add .

hoặc:

git add -A

để tránh vô tình gom nhiều file vào một commit.

4. Kiểm tra cuối

Sau khi commit xong tất cả file, chạy:

git status --short

Nếu vẫn còn file do task hiện tại thay đổi nhưng chưa được commit, tiếp tục xử lý từng file.

Sau đó chạy:

git log --oneline -10

để xác minh các commit vừa tạo.

5. Push

Nếu tất cả commit thành công và repository đã có remote/upstream hợp lệ, chạy:

git push

Chỉ push một lần sau khi toàn bộ commit đã hoàn thành.

Nếu push thất bại:

Không sửa lịch sử commit
Không force push
Báo lại lỗi
Giữ nguyên các commit local
Quy tắc an toàn
Không dùng git reset --hard
Không dùng git checkout -- .
Không dùng git restore .
Không dùng git clean -fd
Không dùng git push --force
Không amend commit cũ
Không sửa hoặc xóa thay đổi của người dùng
Không commit file .env, secret, token, API key hoặc credential
Nếu phát hiện file chứa secret, bỏ qua file đó và cảnh báo
Kết quả cuối cùng

Sau khi hoàn thành, báo cáo:

Số file đã commit
Danh sách commit hash + commit message
File nào chưa commit nếu có
Trạng thái push

Không tạo thêm thay đổi source code chỉ để phục vụ việc commit.
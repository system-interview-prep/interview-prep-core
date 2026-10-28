"""Expected points for seed questions, in Vietnamese (every session runs in vi-VN).

The grader scores against these instead of inventing its own ideal answer, so a
complete answer can reach 9-10. CORE points are what a correct answer must
contain; BONUS points only lift an already complete answer. "CRITICAL" is not
used: the publish contract requires a verified source for it.
"""
# ruff: noqa: E501

from __future__ import annotations

EXPECTED_POINTS_VI: dict[str, list[tuple[str, str, str]]] = {
    # JavaScript / Node.js
    "javascript-closures-in-loops": [
        ("captures-reference", "Closure giữ tham chiếu tới biến ở scope ngoài, không sao chép giá trị lúc tạo hàm.", "CORE"),
        ("var-function-scope", "var có phạm vi hàm nên cả vòng lặp chỉ có một biến; callback chạy sau đều thấy giá trị cuối.", "CORE"),
        ("fix-let-or-iife", "Cách sửa: let tạo binding mới cho mỗi vòng lặp; trước ES6 thì bọc IIFE hoặc truyền giá trị qua tham số.", "CORE"),
        ("example", "Có ví dụ cụ thể như setTimeout trong vòng for in ra cùng một giá trị.", "BONUS"),
    ],
    "javascript-promise-error-handling": [
        ("propagation", "Lỗi trong promise chain đi xuống .catch gần nhất; với async/await thì bắt bằng try/catch.", "CORE"),
        ("swallowed-causes", "Lỗi bị nuốt khi quên return promise trong .then hoặc gọi hàm async mà không await (floating promise).", "CORE"),
        ("real-case", "Kể một trường hợp thực tế lỗi bị nuốt và cách phát hiện hoặc khắc phục.", "CORE"),
        ("prevention", "Biện pháp phòng ngừa: lint no-floating-promises, handler unhandledRejection, log tập trung.", "BONUS"),
    ],
    "javascript-event-loop-microtasks": [
        ("classification", "Promise.then / queueMicrotask là microtask; setTimeout, I/O là macrotask.", "CORE"),
        ("ordering", "Toàn bộ microtask queue chạy hết sau mỗi macrotask, nên Promise.then chạy trước setTimeout(0).", "CORE"),
        ("surprise", "Hệ quả thực tế, ví dụ microtask đệ quy làm đói I/O hoặc thứ tự log khác dự đoán.", "CORE"),
        ("node-specifics", "Nhắc process.nextTick hoặc các phase của event loop trong Node.js.", "BONUS"),
    ],
    # SQL
    "sql-aggregation-and-grouping": [
        ("where-before", "WHERE lọc từng dòng trước khi gom nhóm.", "CORE"),
        ("having-after", "HAVING lọc trên kết quả sau GROUP BY, dùng được hàm tổng hợp như COUNT, SUM.", "CORE"),
        ("example", "Có ví dụ chuyển điều kiện giữa WHERE và HAVING làm kết quả thay đổi hoặc gây lỗi.", "CORE"),
        ("performance", "Điều kiện không cần tổng hợp nên đặt ở WHERE để giảm dữ liệu phải gom.", "BONUS"),
    ],
    "sql-n-plus-one-queries": [
        ("definition", "N+1: một query lấy danh sách rồi mỗi phần tử lại chạy thêm một query, thường do ORM lazy load.", "CORE"),
        ("detection", "Phát hiện qua query log, APM hoặc đếm số query mỗi request thấy cùng một câu SELECT lặp lại.", "CORE"),
        ("two-fixes", "Hai cách sửa: JOIN / eager load, hoặc gom ID rồi WHERE id IN (...) / DataLoader.", "CORE"),
        ("trade-off", "Nêu đánh đổi giữa các cách sửa, ví dụ JOIN nhân bản dòng hoặc giới hạn kích thước IN.", "BONUS"),
    ],
    "sql-join-semantics": [
        ("inner", "INNER JOIN chỉ giữ các dòng khớp ở cả hai bảng.", "CORE"),
        ("left", "LEFT JOIN giữ toàn bộ bảng trái, cột bên phải là NULL khi không khớp.", "CORE"),
        ("report-bug", "Ví dụ lỗi báo cáo do chọn sai join, như INNER JOIN làm mất khách chưa có đơn.", "CORE"),
        ("where-on-pitfall", "Lọc bảng phải trong WHERE biến LEFT JOIN thành INNER; điều kiện phải đặt trong ON.", "BONUS"),
    ],
    # JSON
    "json-schema-evolution": [
        ("additive-safe", "Thêm field mới, tùy chọn là an toàn vì client cũ bỏ qua field lạ.", "CORE"),
        ("unsafe-changes", "Không an toàn: đổi tên, đổi kiểu, xóa field hoặc đổi nghĩa field.", "CORE"),
        ("breaking-strategy", "Khi buộc phải phá vỡ: version API và có thời gian deprecate.", "CORE"),
        ("contract-tests", "Kiểm chứng bằng contract test hoặc schema registry.", "BONUS"),
    ],
    "json-parsing-untrusted-input": [
        ("size-limit", "Giới hạn kích thước body trước khi parse.", "CORE"),
        ("schema-validation", "Validate schema ở tầng nhận (Zod / JSON Schema): kiểu, bắt buộc, độ dài, enum.", "CORE"),
        ("reject-unknown", "Từ chối field lạ và chặn khóa nguy hiểm như __proto__ (prototype pollution).", "CORE"),
        ("error-response", "Trả lỗi 400 rõ ràng, không lộ chi tiết nội bộ.", "BONUS"),
    ],
    "json-numeric-precision": [
        ("precision-loss", "Số lớn hơn 2^53 mất chính xác khi JavaScript parse; số thực làm tròn sai tiền.", "CORE"),
        ("money", "Tiền lưu số nguyên theo đơn vị nhỏ nhất hoặc chuỗi decimal.", "CORE"),
        ("large-ids", "ID lớn gửi dạng chuỗi; cần tính toán thì dùng BigInt hoặc thư viện decimal.", "CORE"),
    ],
    # XML
    "xml-namespaces-and-xsd-validation": [
        ("namespace-aware", "Dùng parser hỗ trợ namespace, so khớp theo URI namespace và tên, không dựa vào prefix.", "CORE"),
        ("xsd-at-boundary", "Validate với XSD ngay tầng nhận trước khi xử lý; sai schema thì từ chối và log.", "CORE"),
        ("secure-parser", "Tắt DTD / external entity để chặn XXE.", "CORE"),
        ("schema-versioning", "Quản lý XSD theo version hoặc namespace của đối tác.", "BONUS"),
    ],
    "xml-vs-json-mapping": [
        ("when-xml", "Chọn XML khi đối tác hoặc chuẩn ngành bắt buộc (SOAP, hóa đơn điện tử), cần XSD hoặc chữ ký số.", "CORE"),
        ("lossy-mapping", "Chuyển đổi có thể mất: attribute và element, namespace, thứ tự, mảng một phần tử, kiểu dữ liệu.", "CORE"),
    ],
    "xml-xxe-and-parser-hardening": [
        ("xxe", "XXE: khai báo external entity để đọc file hoặc gọi URL nội bộ.", "CORE"),
        ("entity-expansion", "Entity expansion (billion laughs) làm cạn bộ nhớ.", "CORE"),
        ("hardening", "Chặn bằng cách tắt DTD và external entity, giới hạn kích thước, dùng parser an toàn mặc định.", "CORE"),
    ],
}

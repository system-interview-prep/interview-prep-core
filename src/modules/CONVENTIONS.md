# Module conventions

Mỗi thư mục con là một bounded module và có thể chứa:

```text
<module>/
  __init__.py       # export AppModule/facade công khai
  router.py         # HTTP transport, không chứa business logic
  schemas.py        # request/response contracts
  service.py        # use cases và transaction boundary
  repository.py     # persistence port/implementation
  models.py         # domain/data models
  facade.py         # API Python công khai cho module khác
  events.py         # event/task contracts
```

Không bắt buộc tạo file rỗng khi module chưa cần nó. Import chéo chỉ đi qua
`facade.py`, `schemas.py` hoặc `events.py`; không import repository của module khác.

`core` chứa chính sách dùng toàn ứng dụng. `infrastructure` chứa adapter kỹ thuật,
không chứa quyết định nghiệp vụ. Worker gọi facade/service của module tương ứng.

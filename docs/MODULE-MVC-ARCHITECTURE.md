# Module MVC architecture

The backend is an HTTP API, so its "view" is a response DTO rather than a
server-rendered template. Every bounded module follows this dependency flow:

```text
Controller (HTTP) -> Application service -> Repository / infrastructure port
                         |
                      Domain models
```

## Target module layout

```text
<module>/
  controllers/          # FastAPI request binding and HTTP error translation
  domain/               # entities, invariants and API/domain contracts
  application/          # one application use case per public service
  infrastructure/       # SQLAlchemy, R2, Celery and external HTTP adapters
  facade.py             # stable API for another module or worker
```

Controllers must not contain SQL, direct R2 calls, Celery dispatch, parser
rules, or domain decision logic. Services depend on ports, not FastAPI. A
repository owns SQL for one aggregate. Models contain validation and domain
invariants; API request/response schemas remain explicit Pydantic models.

## Migration policy

1. Add the target layers while retaining the public route and facade imports.
2. Move one use case at a time and cover it with unit tests at the service
   boundary.
3. Update module registration to import its controller directly.
4. Remove obsolete root files in the same change; do not retain compatibility
   aliases that conceal an incomplete migration.

`user_cvs` is the reference migration because it exercises HTTP upload,
storage, a background job, and structured-domain persistence.

## Migration backlog

| Priority | Modules | Current issue |
| --- | --- | --- |
| 1 | `job_descriptions`, `taxonomy`, `auth`, `users` | SQL and authorization flow remain in HTTP routers. |
| 2 | `chat`, `sessions`, `video_calls`, `voice` | Controller calls AI or signalling integration directly. |
| 3 | `notifications`, `scoring` | Small routers; isolate calculation/query services before adding features. |
| Complete reference | `user_cvs` | Controller, model, service, repository and storage adapter are separated. |
| Already partly aligned | `matching` | Has service/facade/contracts; migrate `router.py` to `controllers/` and split schemas into request/response models. |

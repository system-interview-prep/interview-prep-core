# Interview application packages

- `runtime/` handles interview commands, candidate turns, text runtime, and session lifecycle.
- `progress/` handles candidate progress, calibration, skill summaries, and reported real interviews.
- `quality_admin/` handles interviewer feedback/playbook, quality checks, admin actions, and pipeline monitoring.

`interviews.facade` remains the public boundary for other modules. Runtime routes and adapters may import their focused application package directly.

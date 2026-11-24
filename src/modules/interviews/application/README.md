# Interview application packages

- `runtime/` coordinates persisted interview commands, shared Core execution, and session lifecycle for Chat, Voice, and Video.
- `progress/` handles candidate progress, calibration, skill summaries, and reported real interviews.
- `quality_admin/` handles interviewer feedback/playbook, quality checks, admin actions, and pipeline monitoring.

`interviews.facade` remains the public boundary for other modules. See `../ARCHITECTURE.md` for the Core and modality boundaries.

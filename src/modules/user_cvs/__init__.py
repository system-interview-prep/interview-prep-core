"""CV ownership, upload, storage metadata and parse lifecycle module."""

from src.core.module import AppModule


def build_module() -> AppModule:
    from src.modules.user_cvs.controllers.cv_controller import router

    return AppModule(name="user_cvs", router=router)

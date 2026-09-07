from src.core.module import AppModule
from src.modules.auth.router import router


def build_module() -> AppModule:
    return AppModule(name="auth", router=router)

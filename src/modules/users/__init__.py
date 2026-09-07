from src.core.module import AppModule
from src.modules.users.router import router


def build_module() -> AppModule:
    return AppModule(name="users", router=router)

from src.core.module import AppModule
from src.modules.admin.router import router


def build_module() -> AppModule:
    return AppModule(name="admin", router=router)

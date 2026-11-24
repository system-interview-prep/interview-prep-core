from src.core.module import AppModule
from src.modules.admin_users.router import router

def build_module() -> AppModule:
    return AppModule(name="admin_users", router=router)

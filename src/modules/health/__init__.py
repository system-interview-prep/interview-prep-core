from src.core.module import AppModule


def build_module() -> AppModule:
    from src.modules.health.router import router

    return AppModule(name="health", router=router)

from src.core.module import AppModule


def build_module() -> AppModule:
    from src.modules.taxonomy.router import router
    return AppModule(name="taxonomy", router=router)

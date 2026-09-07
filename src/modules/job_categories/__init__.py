from src.core.module import AppModule
from src.modules.job_categories.router import router


def build_module() -> AppModule:
    return AppModule(name="job_categories", router=router)

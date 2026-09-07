from dataclasses import dataclass

from fastapi import APIRouter


@dataclass(frozen=True, slots=True)
class AppModule:
    """Explicit module registration replaces NestJS AppModule imports."""

    name: str
    router: APIRouter

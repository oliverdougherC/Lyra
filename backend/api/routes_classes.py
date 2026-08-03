"""Class workspace endpoints.

Handlers are sync `def`: `sqlite3` blocks, and FastAPI runs sync handlers in a
threadpool, which is exactly where blocking work belongs.
"""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, status

from backend.api.schemas import ClassCreate, ClassRead, ClassUpdate
from backend.core import classes
from backend.storage.database import get_db

router = APIRouter(prefix="/api", tags=["classes"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]


@router.get("/classes", response_model=list[ClassRead])
def read_classes(conn: DbConn) -> list[dict[str, object]]:
    return classes.list_classes(conn)


@router.post("/classes", response_model=ClassRead, status_code=status.HTTP_201_CREATED)
def create_class(payload: ClassCreate, conn: DbConn) -> dict[str, object]:
    return classes.create_class(conn, payload.name, payload.code, payload.semester)


@router.get("/classes/{class_id}", response_model=ClassRead)
def read_class(class_id: int, conn: DbConn) -> dict[str, object]:
    return classes.get_class(conn, class_id)


@router.patch("/classes/{class_id}", response_model=ClassRead)
def update_class(class_id: int, payload: ClassUpdate, conn: DbConn) -> dict[str, object]:
    return classes.update_class(conn, class_id, **payload.model_dump(exclude_unset=True))


@router.delete("/classes/{class_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_class(class_id: int, conn: DbConn) -> None:
    classes.delete_class(conn, class_id)

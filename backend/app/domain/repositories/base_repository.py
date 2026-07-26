"""Generic CRUD repository shared by every domain aggregate.

Backwards compatible with V1.0 (``get``, ``get_all``, ``create``, ``update``,
``delete``). V1.1 adds counting, existence checks, bulk create and a safer
``update`` that no longer relies on iterating ``__dict__``.
"""

from __future__ import annotations

from typing import Any, Dict, Generic, List, Optional, Sequence, Type, TypeVar

from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

ModelType = TypeVar("ModelType")
CreateSchemaType = TypeVar("CreateSchemaType", bound=PydanticBaseModel)
UpdateSchemaType = TypeVar("UpdateSchemaType", bound=PydanticBaseModel)


class BaseRepository(Generic[ModelType, CreateSchemaType, UpdateSchemaType]):
    def __init__(self, model: Type[ModelType]):
        self.model = model

    # -- reads -------------------------------------------------------------- #
    def get(self, db: Session, id: int) -> Optional[ModelType]:
        return db.query(self.model).filter(self.model.id == id).first()

    def get_all(self, db: Session, skip: int = 0, limit: int = 100) -> List[ModelType]:
        return db.query(self.model).offset(skip).limit(limit).all()

    def count(self, db: Session) -> int:
        return db.query(self.model).count()

    def exists(self, db: Session, id: int) -> bool:
        return (
            db.query(self.model.id).filter(self.model.id == id).first() is not None
        )

    # -- writes ------------------------------------------------------------- #
    def create(self, db: Session, obj_in: CreateSchemaType) -> ModelType:
        obj_in_data = obj_in.model_dump()
        db_obj = self.model(**obj_in_data)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def create_many(
        self, db: Session, objs_in: Sequence[CreateSchemaType]
    ) -> List[ModelType]:
        db_objs = [self.model(**obj.model_dump()) for obj in objs_in]
        db.add_all(db_objs)
        db.commit()
        for obj in db_objs:
            db.refresh(obj)
        return db_objs

    def update(
        self, db: Session, db_obj: ModelType, obj_in: UpdateSchemaType | Dict[str, Any]
    ) -> ModelType:
        update_data = (
            obj_in if isinstance(obj_in, dict) else obj_in.model_dump(exclude_unset=True)
        )
        columns = {c.key for c in sa_inspect(self.model).mapper.column_attrs}
        for field, value in update_data.items():
            if field in columns:
                setattr(db_obj, field, value)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def delete(self, db: Session, id: int) -> Optional[ModelType]:
        obj = db.get(self.model, id)
        if obj is None:
            return None
        db.delete(obj)
        db.commit()
        return obj

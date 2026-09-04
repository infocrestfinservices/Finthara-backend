import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from config import settings

# Fetch database URL from settings or environment
_url = settings.DATABASE_URL

_engine_kwargs = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

if _url.startswith("sqlite"):
    # SQLite configuration for local testing
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL / Supabase Fixes:
    # 1. Fix old "postgres://" protocol if present
    if _url.startswith("postgres://"):
        _url = _url.replace("postgres://", "postgresql://", 1)
    
    # 2. Force SSL mode and explicit 10s connect timeout to avoid hanging requests
    _engine_kwargs["connect_args"] = {
        "sslmode": "require",
        "connect_timeout": 10
    }

engine = create_engine(_url, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Base.metadata.create_all (called on every startup, see main.py) creates tables that don't
# exist yet but never alters one that already does — so a column added to a model after the
# table was first created needs its own ALTER TABLE. There is no Alembic in this project (see
# grant_admin.py), and the person running this deploy is not going to SSH in and run a
# migration script by hand, so this runs the ALTERs itself, once, idempotently, right after
# create_all. SQLite (local/dev) skips it: ADD COLUMN IF NOT EXISTS is Postgres syntax, and a
# fresh create_all already gives a dev database every column anyway.
def ensure_columns(table: str, columns: dict[str, str]) -> None:
    """columns: {column_name: 'SQL TYPE DEFAULT ...'} — run once at startup."""
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import text
    with engine.connect() as con:
        for name, ddl in columns.items():
            con.execute(text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{name}" {ddl}'))
        con.commit()
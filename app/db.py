"""Database connection (PostgreSQL via SQLAlchemy)."""

from sqlalchemy import create_engine, text

from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None


def check_database() -> dict:
    """Try a tiny query and report whether the database answered."""
    if engine is None:
        return {"ok": False, "error": "DATABASE_URL is not set in the .env file."}
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT current_database(), current_setting('server_version')")
            ).one()
        return {"ok": True, "database": row[0], "version": row[1]}
    except Exception as exc:  # show any connection problem on the page
        return {"ok": False, "error": str(exc).splitlines()[0]}

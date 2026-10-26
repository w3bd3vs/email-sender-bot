"""
database.py
===========

Everything related to persistence lives here:

* reading configuration from environment variables
* the SQLAlchemy model for the single `user_configs` table
* Fernet encryption / decryption of secrets
* CRUD helpers (add / list / get / delete / set-default)
* the "default config" rules implemented as single transactions

The rest of the app never touches SQL directly - it calls the functions in
this module. That keeps bot.py focused on Telegram and email_sender.py
focused on talking to mail servers.

All functions are `async` because the database driver (psycopg) is async.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional, Sequence

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    delete,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Load .env for local development. On Render the variables already exist in
# the process environment, so this is a harmless no-op there.
load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    """Return an environment variable or raise a clear error if it is missing."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"See .env.example for the expected values."
        )
    return value


TELEGRAM_BOT_TOKEN: str = _require_env("TELEGRAM_BOT_TOKEN")
CONFIG_ENC_KEY: str = _require_env("CONFIG_ENC_KEY")


def _parse_allowed_users(raw: str) -> set[int]:
    """Turn "111,222, 333" into {111, 222, 333}, ignoring blanks/bad values."""
    allowed: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            allowed.add(int(chunk))
        except ValueError:
            logger.warning("Ignoring non-numeric entry in ALLOWED_USER_IDS: %r", chunk)
    return allowed


ALLOWED_USER_IDS: set[int] = _parse_allowed_users(_require_env("ALLOWED_USER_IDS"))


def _normalize_database_url(url: str) -> str:
    """
    SQLAlchemy's async engine needs an explicit driver in the URL.

    Render (and Heroku) hand out `postgres://...` or `postgresql://...`.
    We rewrite those to `postgresql+psycopg://...`. If the URL already
    contains a driver (e.g. `postgresql+psycopg://`) we leave it alone.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL: str = _normalize_database_url(_require_env("DATABASE_URL"))


# ---------------------------------------------------------------------------
# 2. Encryption helpers (Fernet)
# ---------------------------------------------------------------------------

try:
    _fernet = Fernet(CONFIG_ENC_KEY.encode())
except (ValueError, TypeError) as exc:  # malformed key
    raise RuntimeError(
        "CONFIG_ENC_KEY is not a valid Fernet key. Generate one with: "
        'python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())"'
    ) from exc


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a password / API key. Returns a URL-safe base64 string."""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """
    Decrypt a stored secret.

    Raises `SecretDecryptionError` if the key changed or the data is corrupt,
    so callers can show a friendly message instead of crashing.
    """
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise SecretDecryptionError(
            "Stored secret could not be decrypted. CONFIG_ENC_KEY may have changed."
        ) from exc


class SecretDecryptionError(Exception):
    """Raised when a stored secret cannot be decrypted with the current key."""


# ---------------------------------------------------------------------------
# 3. Engine / session factory
# ---------------------------------------------------------------------------

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
  
)

SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# ---------------------------------------------------------------------------
# 4. Model
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class UserConfig(Base):
    """
    One row per (user, type). A user can have at most one SMTP row and one
    API row, and at most one row flagged as default.
    """

    __tablename__ = "user_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # "smtp" | "api"
    sender_address: Mapped[str] = mapped_column(String(320), nullable=False)

    # SMTP-only fields
    smtp_host: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    smtp_username: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    # Encrypted SMTP password OR API key
    secret_encrypted: Mapped[str] = mapped_column(String(1024), nullable=False)

    # API-only field: "brevo" | "resend" | "sendgrid"
    api_provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # Rule 1: at most one config per (user, type)
        UniqueConstraint("user_id", "type", name="uq_user_configs_user_type"),
        # Rule 2: at most one default per user (partial unique index)
        Index(
            "uq_user_configs_one_default",
            "user_id",
            unique=True,
            postgresql_where=text("is_default = true"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"<UserConfig id={self.id} user={self.user_id} type={self.type} "
            f"default={self.is_default}>"
        )


async def init_db() -> None:
    """Create tables/indexes if they do not exist. Safe to call on every boot."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema is ready.")


async def close_db() -> None:
    """Dispose of the connection pool on shutdown."""
    await engine.dispose()


# ---------------------------------------------------------------------------
# 5. CRUD helpers
# ---------------------------------------------------------------------------

async def list_configs(user_id: int) -> list[UserConfig]:
    """Return all configs for a user, ordered SMTP first then API."""
    async with SessionFactory() as session:
        result = await session.execute(
            select(UserConfig)
            .where(UserConfig.user_id == user_id)
            .order_by(UserConfig.type.desc())  # "smtp" > "api" alphabetically
        )
        return list(result.scalars().all())


async def get_config(user_id: int, config_type: str) -> Optional[UserConfig]:
    """Return the user's config of a given type, or None."""
    async with SessionFactory() as session:
        result = await session.execute(
            select(UserConfig).where(
                UserConfig.user_id == user_id, UserConfig.type == config_type
            )
        )
        return result.scalar_one_or_none()


async def get_default_config(user_id: int) -> Optional[UserConfig]:
    """Return the user's default config, or None if they have no configs."""
    async with SessionFactory() as session:
        result = await session.execute(
            select(UserConfig).where(
                UserConfig.user_id == user_id, UserConfig.is_default.is_(True)
            )
        )
        return result.scalar_one_or_none()


async def save_config(
    *,
    user_id: int,
    config_type: str,
    sender_address: str,
    secret_plaintext: str,
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
    smtp_username: Optional[str] = None,
    api_provider: Optional[str] = None,
) -> UserConfig:
    """
    Insert or replace a config for (user_id, config_type).

    The secret is encrypted here - callers must pass plaintext.

    Default logic (all inside one transaction):
      * If the user has no configs at all, the new one becomes default.
      * If the user already has a default, the new one keeps is_default=False.
      * If the user has configs but none is default (should not normally
        happen), the new one becomes default.

    Because of the partial unique index, we must clear the old default
    *before* setting a new one, otherwise the insert would violate it.
    """
    encrypted = encrypt_secret(secret_plaintext)

    async with SessionFactory() as session:
        async with session.begin():  # single transaction
            # Lock the user's rows so two concurrent saves cannot race.
            existing = (
                await session.execute(
                    select(UserConfig)
                    .where(UserConfig.user_id == user_id)
                    .with_for_update()
                )
            ).scalars().all()

            has_default = any(cfg.is_default for cfg in existing)
            should_be_default = not has_default

            values = {
                "user_id": user_id,
                "type": config_type,
                "sender_address": sender_address,
                "smtp_host": smtp_host,
                "smtp_port": smtp_port,
                "smtp_username": smtp_username,
                "secret_encrypted": encrypted,
                "api_provider": api_provider,
                "is_default": should_be_default,
            }

            # Upsert on the (user_id, type) unique constraint.
            stmt = (
                pg_insert(UserConfig)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_user_configs_user_type",
                    set_={
                        "sender_address": values["sender_address"],
                        "smtp_host": values["smtp_host"],
                        "smtp_port": values["smtp_port"],
                        "smtp_username": values["smtp_username"],
                        "secret_encrypted": values["secret_encrypted"],
                        "api_provider": values["api_provider"],
                        # Keep the existing default flag when updating.
                        "is_default": UserConfig.is_default,
                    },
                )
                .returning(UserConfig)
            )
            result = await session.execute(stmt)
            saved = result.scalar_one()

        # `session.begin()` commits on exit; refresh outside the block.
        await session.refresh(saved)
        return saved


async def set_default(user_id: int, config_type: str) -> None:
    """
    Make (user_id, config_type) the default.

    Both statements run in one transaction so the partial unique index is
    never violated and there is never a moment with zero defaults.
    """
    async with SessionFactory() as session:
        async with session.begin():
            # 1) Unset the current default (if any).
            await session.execute(
                update(UserConfig)
                .where(UserConfig.user_id == user_id, UserConfig.is_default.is_(True))
                .values(is_default=False)
            )
            # 2) Set the requested one.
            await session.execute(
                update(UserConfig)
                .where(
                    UserConfig.user_id == user_id,
                    UserConfig.type == config_type,
                )
                .values(is_default=True)
            )


async def delete_config(user_id: int, config_type: str) -> bool:
    """
    Delete a config. Returns True if a row was deleted.

    If the deleted row was the default, the remaining config (if any) is
    promoted to default - in the same transaction.
    """
    async with SessionFactory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    select(UserConfig)
                    .where(
                        UserConfig.user_id == user_id,
                        UserConfig.type == config_type,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if row is None:
                return False

            was_default = row.is_default
            await session.execute(
                delete(UserConfig).where(UserConfig.id == row.id)
            )

            if was_default:
                # Promote whatever is left (there can be at most one row).
                remaining = (
                    await session.execute(
                        select(UserConfig)
                        .where(UserConfig.user_id == user_id)
                        .order_by(UserConfig.created_at.asc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if remaining is not None:
                    await session.execute(
                        update(UserConfig)
                        .where(UserConfig.id == remaining.id)
                        .values(is_default=True)
                    )
            return True


async def count_configs(user_id: int) -> int:
    """Small helper used by the settings screen."""
    configs: Sequence[UserConfig] = await list_configs(user_id)
    return len(configs)

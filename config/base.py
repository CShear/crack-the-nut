"""Reusable pydantic-settings base for trading bots."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Literal


class BotSettings(BaseSettings):
    """Base settings every bot should extend.

    Loads from .env by default. Subclass and add exchange-specific fields.

    Example::

        class MyBotSettings(BotSettings):
            exchange_api_key: str = ""
            custom_param: float = 1.0
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Environment
    environment: Literal["development", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_logs: bool = False

    # Trading core
    bankroll: float = Field(default=1000.0, ge=0, description="Starting bankroll in USD")
    paper_trade: bool = True

    # Risk parameters
    max_position_size_pct: float = Field(default=0.05, ge=0.01, le=0.50)
    max_total_exposure_pct: float = Field(default=0.50, ge=0.10, le=1.0)
    max_positions: int = Field(default=10, ge=1)
    max_daily_loss_pct: float = Field(default=0.05, ge=0.01, le=0.50)
    kill_switch_pct: float = Field(default=0.20, ge=0.05, le=1.0)
    min_order_notional: float = Field(default=10.0, ge=0)

    # Telegram notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Scheduler
    scheduler_timezone: str = "UTC"

    # Database
    database_path: str = "data/bot.db"

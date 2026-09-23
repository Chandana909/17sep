"""Builds the configured ModelGateway. Switching model/provider is a config change only."""

from __future__ import annotations

from asas.adapters.openai_compat import OpenAICompatibleGateway
from asas.adapters.replay import ReplayGateway
from asas.agents.gateway import ModelGateway
from asas.config import Config, ConfigMissing


def build_gateway(cfg: Config) -> ModelGateway | None:
    """None whenever agents are disabled or misconfigured: reports use templates (rail 15)."""
    try:
        if cfg.require("agents", "enabled") is not True:
            return None
        provider = cfg.require("agents", "provider")
        if provider == "openai_compatible":
            return OpenAICompatibleGateway(
                base_url=str(cfg.require("agents", "base_url")),
                model=str(cfg.require("agents", "model_id")),
                api_key_env=cfg.data["agents"].get("api_key_env"),
                timeout_seconds=cfg.integer("agents", "timeout_seconds"),
            )
        if provider == "replay":
            return ReplayGateway.from_file(str(cfg.require("agents", "replay_file")))
    except ConfigMissing:
        return None
    return None

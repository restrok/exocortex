"""Config-driven multi-user spaces management for Exocortex Brain."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

DEFAULT_SPACES_CONFIG: dict[str, Any] = {
    "version": 1,
    "spaces": {
        "personal-fsirio": {
            "type": "personal",
            "owner": "fsirio",
            "description": "Espacio personal de fsirio",
        },
        "personal-mercedes": {
            "type": "personal",
            "owner": "mercedes",
            "description": "Espacio personal de mercedes",
        },
        "shared": {
            "type": "shared",
            "owner": None,
            "description": "Espacio compartido familiar",
        },
        "work": {
            "type": "work",
            "owner": "fsirio",
            "description": "Espacio de trabajo general",
        },
    },
    "users": {
        "fsirio": [
            "personal-fsirio",
            "shared",
            "work",
        ],
        "mercedes": [
            "personal-mercedes",
            "shared",
        ],
    },
}

_CONFIG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def get_spaces_config_path(
    data_dir: Path | None = None, config_path: Path | None = None
) -> Path:
    """Return the resolved path to spaces.yaml."""
    if config_path is not None:
        return config_path
    base_dir = data_dir or Path("brain")
    return base_dir / "spaces.yaml"


def load_spaces_config(
    data_dir: Path | None = None, config_path: Path | None = None
) -> dict[str, Any]:
    """Load spaces.yaml or initialize with default config if missing."""
    target_path = get_spaces_config_path(data_dir=data_dir, config_path=config_path)
    str_path = str(target_path.resolve())

    if target_path.exists():
        try:
            mtime = target_path.stat().st_mtime
            if str_path in _CONFIG_CACHE:
                cached_mtime, cached_data = _CONFIG_CACHE[str_path]
                if cached_mtime == mtime:
                    return cached_data

            content = target_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(content)
            if isinstance(parsed, dict) and "spaces" in parsed and "users" in parsed:
                _CONFIG_CACHE[str_path] = (mtime, parsed)
                return parsed
        except Exception as e:
            logger.warning("Error reading %s: %s; using default config", target_path, e)

    # Initialize default config file if it does not exist
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = target_path.with_suffix(".yaml.tmp")
        tmp_target.write_text(
            yaml.safe_dump(DEFAULT_SPACES_CONFIG, sort_keys=False),
            encoding="utf-8",
        )
        tmp_target.replace(target_path)
        mtime = target_path.stat().st_mtime
        _CONFIG_CACHE[str_path] = (mtime, DEFAULT_SPACES_CONFIG)
    except Exception as e:
        logger.debug("Could not write default spaces.yaml to %s: %s", target_path, e)

    return DEFAULT_SPACES_CONFIG


def resolve_allowed_spaces(
    user_id: str | None,
    space_id: str | None = None,
    data_dir: Path | None = None,
    config_path: Path | None = None,
) -> list[str]:
    """Resolve allowed spaces for a user from configuration."""
    normalized_user = user_id.strip().lower() if user_id else None
    config = load_spaces_config(data_dir=data_dir, config_path=config_path)
    users_map: dict[str, list[str]] = config.get("users", {})

    if normalized_user and normalized_user in users_map:
        allowed = list(users_map[normalized_user])
    elif normalized_user:
        # User not explicitly listed in config: fallback to personal + shared
        spaces_map: dict[str, dict[str, Any]] = config.get("spaces", {})
        personal_space = f"personal-{normalized_user}"
        allowed = []
        if personal_space in spaces_map:
            allowed.append(personal_space)
        if "shared" in spaces_map:
            allowed.append("shared")
        if not allowed:
            allowed = ["work"]
    else:
        allowed = ["work"]

    if space_id is not None:
        target = space_id.strip().lower()
        if target == "personal":
            target = f"personal-{normalized_user}" if normalized_user else "work"
        if target in allowed:
            return [target]
        return []
    return allowed


def resolve_remember_space(
    user_id: str | None,
    space_id: str | None = None,
    default_space: str = "work",
    data_dir: Path | None = None,
    config_path: Path | None = None,
) -> str:
    """Resolve the destination space for memory creation deterministically."""
    normalized_user = user_id.strip().lower() if user_id else None
    allowed = resolve_allowed_spaces(
        user_id=normalized_user,
        space_id=None,
        data_dir=data_dir,
        config_path=config_path,
    )

    if space_id is not None:
        target = space_id.strip().lower()
        if target == "personal":
            candidate = (
                f"personal-{normalized_user}" if normalized_user else default_space
            )
            return candidate if candidate in allowed else default_space
        if target in allowed:
            return target
        if normalized_user and f"personal-{normalized_user}" in allowed:
            return f"personal-{normalized_user}"
        return default_space

    if normalized_user == "mercedes":
        return "personal-mercedes" if "personal-mercedes" in allowed else default_space
    if normalized_user == "fsirio":
        return default_space if default_space in allowed else "personal-fsirio"
    if default_space in allowed:
        return default_space
    return allowed[0] if allowed else default_space


def list_spaces_for_user(
    user_id: str | None,
    data_dir: Path | None = None,
    config_path: Path | None = None,
) -> list[dict[str, Any]]:
    """List knowledge spaces available to user with metadata."""
    allowed = resolve_allowed_spaces(
        user_id=user_id,
        space_id=None,
        data_dir=data_dir,
        config_path=config_path,
    )
    config = load_spaces_config(data_dir=data_dir, config_path=config_path)
    spaces_map: dict[str, dict[str, Any]] = config.get("spaces", {})

    result: list[dict[str, Any]] = []
    for s_name in allowed:
        info = spaces_map.get(s_name, {})
        result.append(
            {
                "name": s_name,
                "type": info.get("type", "work"),
                "owner": info.get("owner"),
                "description": info.get("description", ""),
            }
        )
    return result


def create_space(
    data_dir: Path,
    name: str,
    owner: str,
    space_type: str = "work",
    description: str = "",
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Create a new space in configuration and initialize its directory in Vault."""
    normalized_name = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
    if not normalized_name:
        raise ValueError("Space name must contain valid alphanumeric characters")

    normalized_owner = owner.strip().lower()
    valid_types = ("work", "personal", "shared")
    if space_type not in valid_types:
        raise ValueError(
            f"Invalid space type '{space_type}'. Must be one of {valid_types}"
        )

    target_path = get_spaces_config_path(data_dir=data_dir, config_path=config_path)
    config = load_spaces_config(data_dir=data_dir, config_path=target_path)

    spaces = config.setdefault("spaces", {})
    users = config.setdefault("users", {})

    spaces[normalized_name] = {
        "type": space_type,
        "owner": normalized_owner if space_type != "shared" else None,
        "description": description or f"Espacio {normalized_name}",
    }

    user_spaces = users.setdefault(normalized_owner, [])
    if normalized_name not in user_spaces:
        user_spaces.append(normalized_name)

    if space_type == "shared":
        for _u_name, s_list in users.items():
            if normalized_name not in s_list:
                s_list.append(normalized_name)

    # Persist updated configuration
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(".tmp")
    tmp_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    tmp_path.replace(target_path)

    # Invalidate memory cache
    _CONFIG_CACHE[str(target_path.resolve())] = (target_path.stat().st_mtime, config)

    # Create directory in Vault
    vault_space_dir = data_dir / "Vault" / normalized_name
    vault_space_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Created space '%s' (type: %s, owner: %s)",
        normalized_name,
        space_type,
        normalized_owner,
    )
    return {
        "status": "ok",
        "space": normalized_name,
        "owner": normalized_owner,
        "type": space_type,
        "path": str(vault_space_dir),
    }

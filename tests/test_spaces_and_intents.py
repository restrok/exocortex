"""Unit tests for config-driven spaces and proactive intent MCP tools."""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from exocortex.mcp_server import create_server
from exocortex.service import BrainService
from exocortex.spaces import (
    create_space,
    list_spaces_for_user,
    load_spaces_config,
    resolve_allowed_spaces,
    resolve_remember_space,
)
from tests.conftest import make_settings


def test_spaces_config_defaults_and_resolution(tmp_path: Path) -> None:
    """Default spaces config initializes properly and resolves expected user spaces."""
    data_dir = tmp_path / "brain"
    data_dir.mkdir(parents=True, exist_ok=True)

    config = load_spaces_config(data_dir=data_dir)
    assert "spaces" in config
    assert "users" in config

    # fsirio sees personal-fsirio, shared, work
    fsirio_spaces = resolve_allowed_spaces("fsirio", data_dir=data_dir)
    assert fsirio_spaces == ["personal-fsirio", "shared", "work"]

    # mercedes sees personal-mercedes, shared
    mercedes_spaces = resolve_allowed_spaces("mercedes", data_dir=data_dir)
    assert mercedes_spaces == ["personal-mercedes", "shared"]

    # specific target resolution
    mercedes_personal = resolve_allowed_spaces(
        "mercedes", space_id="personal", data_dir=data_dir
    )
    assert mercedes_personal == ["personal-mercedes"]
    assert resolve_allowed_spaces("mercedes", space_id="work", data_dir=data_dir) == []
    fsirio_work = resolve_allowed_spaces("fsirio", space_id="work", data_dir=data_dir)
    assert fsirio_work == ["work"]

    # destination remember resolution
    assert resolve_remember_space("mercedes", data_dir=data_dir) == "personal-mercedes"
    assert resolve_remember_space("fsirio", data_dir=data_dir) == "work"
    assert (
        resolve_remember_space("mercedes", space_id="shared", data_dir=data_dir)
        == "shared"
    )
    assert (
        resolve_remember_space("mercedes", space_id="work", data_dir=data_dir)
        == "personal-mercedes"
    )


def test_create_space_updates_yaml_and_creates_folder(tmp_path: Path) -> None:
    """create_space writes to spaces.yaml and initializes Vault/<space>/."""
    data_dir = tmp_path / "brain"
    data_dir.mkdir(parents=True, exist_ok=True)

    res = create_space(
        data_dir=data_dir,
        name="work-dataart",
        owner="fsirio",
        space_type="work",
        description="DataArt contract projects",
    )
    assert res["status"] == "ok"
    assert res["space"] == "work-dataart"

    # Verify directory created in Vault
    expected_dir = data_dir / "Vault" / "work-dataart"
    assert expected_dir.is_dir()

    # Verify spaces.yaml updated
    config_file = data_dir / "spaces.yaml"
    assert config_file.exists()
    content = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert "work-dataart" in content["spaces"]
    assert "work-dataart" in content["users"]["fsirio"]
    assert "work-dataart" not in content["users"]["mercedes"]

    # Verify list_spaces_for_user includes new space for fsirio
    fsirio_list = list_spaces_for_user("fsirio", data_dir=data_dir)
    assert any(s["name"] == "work-dataart" for s in fsirio_list)

    mercedes_list = list_spaces_for_user("mercedes", data_dir=data_dir)
    assert not any(s["name"] == "work-dataart" for s in mercedes_list)


def test_brain_service_intent_lifecycle(tmp_path: Path) -> None:
    """BrainService creates, retrieves, and updates proactive intent notes."""
    settings = make_settings(tmp_path / "brain")
    service = BrainService(settings)

    # 1. Register intent
    note = service.register_intent(
        title="Check Sunday BBQ Weather",
        goal_description='{"action": "check_rain_and_wind"}',
        target_event_timestamp="2026-09-30T12:00:00-03:00",
        decision_horizon_hours=24,
        eval_tool_target="weather_check",
        eval_params={"location": "Tigre"},
        space_id="personal-fsirio",
        owner="fsirio",
    )
    assert note.metadata.type == "proactive_intent"
    assert note.metadata.space_id == "personal-fsirio"
    assert "status: active" in note.content

    intent_id = str(note.metadata.id)

    # 2. Get intent with allowed spaces
    retrieved = service.get_intent(
        intent_id, allowed_spaces=["personal-fsirio", "shared"]
    )
    assert retrieved is not None
    assert retrieved.metadata.title == "Check Sunday BBQ Weather"

    # Isolation check: user without access cannot see it
    denied = service.get_intent(
        intent_id, allowed_spaces=["personal-mercedes", "shared"]
    )
    assert denied is None

    # 3. Update intent status
    updated = service.update_intent_status(
        intent_id=intent_id,
        status="notified",
        context_data={"forecast": "sunny"},
        allowed_spaces=["personal-fsirio"],
    )
    assert updated is not None
    assert "status: notified" in updated.content
    assert "sunny" in updated.content


def test_mcp_intent_and_spaces_tools(tmp_path: Path) -> None:
    """MCP server tools for spaces and proactive intents function end-to-end."""
    settings = make_settings(tmp_path / "brain")
    server = create_server(settings)

    def call(tool_name: str, **arguments):
        res = server._tool_manager.get_tool(tool_name).fn(**arguments)
        if asyncio.iscoroutine(res):
            return asyncio.run(res)
        return res

    # 1. brain_list_spaces
    list_res = call("brain_list_spaces", user_id="fsirio")
    assert list_res["status"] == "ok"
    spaces_names = [s["name"] for s in list_res["data"]["spaces"]]
    assert "work" in spaces_names
    assert "personal-fsirio" in spaces_names

    # 2. brain_create_space
    create_res = call(
        "brain_create_space",
        name="work-consultora-x",
        owner="fsirio",
        space_type="work",
        description="Client X consultancy",
    )
    assert create_res["status"] == "ok"
    assert create_res["data"]["space"] == "work-consultora-x"

    # 3. brain_register_intent
    reg_res = call(
        "brain_register_intent",
        title="Recordatorio Service Auto",
        goal_description="Turno service Honda",
        target_event_timestamp="2026-10-01T08:00:00-03:00",
        space_id="shared",
        user_id="fsirio",
    )
    assert reg_res["status"] == "ok"
    intent_id = reg_res["data"]["intent_id"]
    assert intent_id is not None

    # 4. brain_get_intent
    # Accessible to mercedes because it is shared
    get_by_mercedes = call("brain_get_intent", intent_id=intent_id, user_id="mercedes")
    assert get_by_mercedes["status"] == "ok"
    assert get_by_mercedes["data"]["id"] == intent_id

    # 5. brain_update_intent_status
    upd_res = call(
        "brain_update_intent_status",
        intent_id=intent_id,
        status="executed",
        context_data={"executed_at": "2026-10-01T08:05:00"},
        user_id="fsirio",
    )
    assert upd_res["status"] == "ok"
    assert upd_res["data"]["status"] == "executed"

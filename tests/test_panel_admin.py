from __future__ import annotations

from typing import Any
from uuid import uuid4

from bedolaga_grace_bridge.panel_admin import PanelAdminClient, _collection, _unwrap


class RecordingPanelClient(PanelAdminClient):
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        super().__init__("https://panel.example.com", "hidden-token")
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        self.requests.append((method, path, payload))
        return self.responses[(method, path)]


def test_response_helpers_support_wrapped_collections() -> None:
    payload = {"response": {"internalSquads": [{"uuid": "one"}]}}
    assert _unwrap({"response": 42}) == 42
    assert _collection(payload, "internalSquads") == [{"uuid": "one"}]


def test_list_inbounds_maps_panel_contract() -> None:
    inbound_uuid = str(uuid4())
    profile_uuid = str(uuid4())
    client = RecordingPanelClient(
        {
            ("GET", "/api/config-profiles/inbounds"): {
                "response": {
                    "inbounds": [
                        {
                            "uuid": inbound_uuid,
                            "profileUuid": profile_uuid,
                            "tag": "CONTINUITY_XHTTP",
                            "type": "vless",
                            "network": "xhttp",
                            "security": "none",
                            "port": 2443,
                        }
                    ]
                }
            }
        }
    )

    assert client.list_inbounds()[0].uuid == inbound_uuid
    assert client.list_inbounds()[0].profile_uuid == profile_uuid
    assert client.list_inbounds()[0].tag == "CONTINUITY_XHTTP"


def test_create_internal_squad_uses_only_confirmed_inbounds() -> None:
    squad_uuid = str(uuid4())
    inbound_uuids = [str(uuid4()), str(uuid4())]
    client = RecordingPanelClient(
        {("POST", "/api/internal-squads"): {"response": {"uuid": squad_uuid, "name": "Continuity Access"}}}
    )

    squad = client.create_internal_squad("Continuity Access", inbound_uuids)

    assert squad.uuid == squad_uuid
    assert squad.inbound_uuids == tuple(inbound_uuids)
    assert client.requests == [
        (
            "POST",
            "/api/internal-squads",
            {"name": "Continuity Access", "inbounds": inbound_uuids},
        )
    ]

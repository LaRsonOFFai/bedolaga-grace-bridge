from uuid import UUID

from bedolaga_grace_bridge.controller import _selected_for_rollout


def test_rollout_is_stable_and_bounded_for_40000_users() -> None:
    users = [str(UUID(int=index + 1)) for index in range(40_000)]
    selected_5 = {uuid for uuid in users if _selected_for_rollout(uuid, 5)}
    selected_25 = {uuid for uuid in users if _selected_for_rollout(uuid, 25)}
    selected_50 = {uuid for uuid in users if _selected_for_rollout(uuid, 50)}
    selected_100 = {uuid for uuid in users if _selected_for_rollout(uuid, 100)}

    assert len(selected_5) == 2_000
    assert len(selected_25) == 10_000
    assert len(selected_50) == 20_000
    assert len(selected_100) == 40_000
    assert selected_5 < selected_25 < selected_50 < selected_100
    assert selected_25 == {uuid for uuid in users if _selected_for_rollout(uuid, 25)}

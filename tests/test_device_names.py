"""Device names for entries with several selected customers (v0.4.3)."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from custom_components.kepco_on.const import DOMAIN

from .test_sensor import RAW_CUSTOMER_SECRET, RAW_HOUSE_SECRET, bill, customer, setup_entities

GROUP_NAMES = {
    "monthly_usage": "월별 사용량",
    "meter_usage": "검침 정보",
    "usage": "전기사용량·예측",
    "electricity_charge": "확정 전기요금",
    "neighbor_comparison": "이웃 전기사용량 비교",
    "greenhouse_gas": "온실가스 배출량",
}


def hash_key(seed: str) -> str:
    return sha256(seed.encode()).hexdigest()


def identifier(key: str, group: str) -> str:
    return key if group == "meter_usage" else f"{key}:{group}"


async def test_single_customer_keeps_plain_device_names() -> None:
    key = hash_key("single")
    entities = await setup_entities(customers=(customer(key),), bills_by_customer_key={key: bill()})

    assert {entity.device_info["name"] for entity in entities} == set(GROUP_NAMES.values())


async def test_several_customers_get_hash_suffixed_names_with_unchanged_ids() -> None:
    from custom_components.kepco_on.sensor import ACTIVE_SENSOR_KEYS

    keys = (hash_key("first"), hash_key("second"))
    assert keys[0][:4] != keys[1][:4]
    customers = tuple(customer(key) for key in keys)
    entities = await setup_entities(
        customers=customers, bills_by_customer_key={key: bill() for key in keys}
    )

    assert len(entities) == 68
    names: set[str] = set()
    for entity in entities:
        key = entity.customer.stable_key
        group = entity.entity_description.device_group.value
        info: dict[str, Any] = dict(entity.device_info)
        # Identity is exactly what v0.4.2 registered; only the display name changes.
        assert entity.unique_id == f"{key}_{entity.entity_description.key}"
        assert info["identifiers"] == {(DOMAIN, identifier(key, group))}
        assert info["name"] == f"{GROUP_NAMES[group]} ({key[:4]})"
        names.add(info["name"])
        for private in (RAW_CUSTOMER_SECRET, RAW_HOUSE_SECRET, "푸른아파트", "101동", "1001호"):
            assert private not in info["name"]
    assert len(names) == 12
    for key in keys:
        assert {
            entity.entity_description.key
            for entity in entities
            if entity.customer.stable_key == key
        } == ACTIVE_SENSOR_KEYS


def test_suffix_grows_only_as_far_as_needed_to_tell_customers_apart() -> None:
    from custom_components.kepco_on.sensor import _device_name_suffixes

    shared = "abcd1" + "0" * 59
    other = "abcd2" + "0" * 59
    distinct = "ef01" + "0" * 60
    assert _device_name_suffixes([customer(shared)]) == {}
    assert _device_name_suffixes([customer(shared), customer(shared)]) == {}
    assert _device_name_suffixes([customer(shared), customer(distinct)]) == {
        shared: "abcd",
        distinct: "ef01",
    }
    assert _device_name_suffixes([customer(shared), customer(other), customer(distinct)]) == {
        shared: "abcd1",
        other: "abcd2",
        distinct: "ef010",
    }


def test_suffix_never_shows_raw_text_of_a_non_hash_key() -> None:
    from custom_components.kepco_on.sensor import _device_name_suffixes

    # Real keys are integration hashes; any other stored key is hashed, not displayed.
    raw_keys = ("1234567890", "1234567891")
    suffixes = _device_name_suffixes([customer(key) for key in raw_keys])

    assert suffixes == {key: hash_key(key)[:4] for key in raw_keys}
    for key, suffix in suffixes.items():
        assert suffix not in key

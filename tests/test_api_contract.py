from types import SimpleNamespace

import httpx
import pytest
from fastapi.routing import APIRoute

from episode.api.projections import event_origins, semantic_receipt_sources
from episode.api.routes import create_api


def test_event_sources_exclude_transport_receipts_but_keep_integrations():
    receipts = [
        SimpleNamespace(
            source="http:alarm_server",
            metadata={
                "transport": "http",
                "connector_type": "alarm_server",
                "interpretation_source": "hikvision:alarm_server",
                "plugin_id": "hikvision-alarm-server",
            },
        ),
        SimpleNamespace(source="hikvision:isapi", metadata={"transport": "plugin"}),
        SimpleNamespace(source="plugin:hikvision-sdk", metadata={"transport": "plugin"}),
        SimpleNamespace(source="http:unclaimed", metadata={"transport": "http"}),
        SimpleNamespace(source="external:integration", metadata={}),
    ]

    assert semantic_receipt_sources(receipts) == [
        "hikvision:alarm_server",
        "hikvision:isapi",
        "external:integration",
    ]


def test_event_origins_present_one_named_plugin_instead_of_its_delivery_alias():
    receipts = [
        SimpleNamespace(
            source="plugin:hikvision-sdk",
            metadata={
                "transport": "plugin",
                "plugin_id": "hikvision-sdk",
                "interpreted": True,
                "ingress_handlers": [{"id": "hikvision-sdk-events", "state": "claimed"}],
            },
        )
    ]

    sources, origins = event_origins(
        "hikvision:sdk",
        receipts,
        [{"id": "hikvision-sdk", "name": "Hikvision HCNetSDK", "type": "hikvision_sdk"}],
        {"ingress_handler": "hikvision-sdk-events"},
    )

    assert sources == ["hikvision:sdk"]
    assert origins == [
        {
            "kind": "plugin",
            "id": "hikvision-sdk",
            "name": "Hikvision HCNetSDK",
            "source": "hikvision:sdk",
        }
    ]


def test_public_api_has_no_duplicate_routes_and_keeps_expected_surface():
    app = create_api(object())
    route_keys = [
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    ]

    assert len(route_keys) == len(set(route_keys))
    assert set(app.openapi()["paths"]) == {
        "/health",
        "/api/v1/status",
        "/api/v1/diagnostics",
        "/api/v1/diagnostics/export",
        "/api/v1/areas",
        "/api/v1/areas/{area_id}",
        "/api/v1/devices",
        "/api/v1/devices/validate",
        "/api/v1/devices/{device_id}",
        "/api/v1/episodes",
        "/api/v1/episodes/{episode_id}",
        "/api/v1/episodes/{episode_id}/current-views",
        "/api/v1/episodes/{episode_id}/current-views/{device_id}",
        "/api/v1/episodes/{episode_id}/events",
        "/api/v1/episodes/{episode_id}/evidence",
        "/api/v1/episodes/{episode_id}/receipts",
        "/api/v1/episodes/{episode_id}/timelapse",
        "/api/v1/events",
        "/api/v1/events/{event_id}",
        "/api/v1/events/{event_id}/closest-snapshot",
        "/api/v1/events/{event_id}/payload",
        "/api/v1/events/{event_id}/picture",
        "/api/v1/receipts",
        "/api/v1/receipts/{receipt_id}",
        "/api/v1/receipts/{receipt_id}/artifact",
        "/api/v1/evidence",
        "/api/v1/covers",
        "/api/v1/evidence/{evidence_id}",
        "/api/v1/evidence/{evidence_id}/closest-event",
        "/api/v1/evidence/{evidence_id}/file",
    }


def test_public_api_operation_ids_are_unique():
    schema = create_api(object()).openapi()
    operation_ids = [
        operation["operationId"] for path in schema["paths"].values() for operation in path.values()
    ]

    assert len(operation_ids) == len(set(operation_ids))


def test_collection_contract_uses_one_pagination_shape_and_explicit_items():
    schema = create_api(object()).openapi()
    collections = {
        "/api/v1/episodes",
        "/api/v1/events",
        "/api/v1/evidence",
        "/api/v1/receipts",
        "/api/v1/episodes/{episode_id}/events",
        "/api/v1/episodes/{episode_id}/evidence",
        "/api/v1/episodes/{episode_id}/receipts",
    }

    for path in collections:
        operation = schema["paths"][path]["get"]
        parameters = {item["name"]: item for item in operation["parameters"]}
        assert parameters["limit"]["schema"] == {
            "type": "integer",
            "maximum": 500,
            "minimum": 1,
            "description": "Maximum number of items to return.",
            "default": 100,
            "title": "Limit",
        }
        assert parameters["offset"]["schema"]["default"] == 0
        response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert response_schema["type"] == "array"
        assert "$ref" in response_schema["items"]


def test_openapi_declares_success_and_error_response_models():
    schema = create_api(object()).openapi()

    health = schema["paths"]["/health"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    event_errors = schema["paths"]["/api/v1/events/{event_id}"]["get"]["responses"]

    assert health["$ref"].endswith("/HealthResponse")
    assert event_errors["404"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ApiErrorResponse"
    )


@pytest.mark.asyncio
async def test_public_api_uses_stable_error_envelopes():
    app = create_api(object())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/api/v1/not-a-route")
        invalid = await client.get("/api/v1/events?limit=0")

    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "not_found", "message": "Not Found", "details": []}}
    assert invalid.status_code == 422
    body = invalid.json()["error"]
    assert body["code"] == "validation_error"
    assert body["message"] == "Request validation failed"
    assert body["details"][0]["location"] == ["query", "limit"]


@pytest.mark.asyncio
async def test_internal_api_errors_do_not_expose_exception_details():
    app = create_api(object())
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/events")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "An unexpected error occurred",
            "details": [],
        }
    }
    assert "list_events" not in response.text

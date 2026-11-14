"""HTTP-level tests for the SMS send API wrapper."""

import json

import httpx
import pytest

from external_apis import sms_api

BASE_URL = "https://sms.test/ex"


@pytest.fixture
def transport(monkeypatch):
    captured: dict = {}

    def install(handler):
        def recording_handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return handler(request)

        async def fake_client(**options):
            captured["options"] = options
            return httpx.AsyncClient(transport=httpx.MockTransport(recording_handler))

        monkeypatch.setattr(sms_api, "create_async_http_client", fake_client)
        return captured

    monkeypatch.setattr(sms_api, "RESERVATION_API_BASEURL", BASE_URL)
    monkeypatch.setattr(sms_api, "SMS_API_AUTHORIZATION", "")
    return install


def _request_part(request: httpx.Request) -> tuple[dict, str]:
    body = request.read().decode()
    boundary = request.headers["content-type"].split("boundary=")[1]
    part = next(p for p in body.split(f"--{boundary}") if 'name="request"' in p)
    headers, _, value = part.partition("\r\n\r\n")
    return json.loads(value.strip()), headers


async def test_sends_multipart_json_request_part(transport):
    captured = transport(
        lambda _: httpx.Response(
            200,
            json={
                "messageId": "01936c4e-1234-7abc-8def-567890abcdef",
                "messageType": "LMS",
                "sendStatus": "SUCCESS",
                "resultCode": 1,
                "resultMessage": "전송 성공",
            },
        )
    )

    result = await sms_api.send_sms(
        site_id="demo-device-care",
        receiver_phone_number="010-0000-0101",
        subject="주차 안내",
        content="주차 안내입니다.",
    )

    assert result.success is True
    assert result.message_type == "LMS"
    request = captured["request"]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/sms/send"
    assert "Authorization" not in request.headers
    payload, part_headers = _request_part(request)
    assert "application/json" in part_headers
    assert payload == {
        "siteId": "demo-device-care",
        "receiverPhoneNumber": "01000000101",
        "subject": "주차 안내",
        "content": "주차 안내입니다.",
        "testMode": False,
    }


async def test_authorization_from_config(transport, monkeypatch):
    monkeypatch.setattr(sms_api, "SMS_API_AUTHORIZATION", "Bearer token")
    captured = transport(lambda _: httpx.Response(200, json={"sendStatus": "SUCCESS"}))

    await sms_api.send_sms(
        site_id="s", receiver_phone_number="01000000101", content="hi"
    )

    assert captured["request"].headers["Authorization"] == "Bearer token"
    payload, _ = _request_part(captured["request"])
    assert "subject" not in payload


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"sendStatus": "FAIL", "resultCode": -101}, False),
        ({"resultCode": 1}, True),
        ({"resultCode": -101}, False),
    ],
)
async def test_send_status_decides_success(transport, body, expected):
    transport(lambda _: httpx.Response(200, json=body))

    result = await sms_api.send_sms(
        site_id="s", receiver_phone_number="01000000101", content="hi"
    )

    assert result.success is expected


async def test_http_error_returns_failure(transport):
    transport(lambda _: httpx.Response(400, json={"message": "invalid site"}))

    result = await sms_api.send_sms(
        site_id="s", receiver_phone_number="01000000101", content="hi"
    )

    assert result.success is False
    assert result.message == "invalid site"


async def test_transport_error_returns_failure(transport):
    def boom(_):
        raise httpx.ConnectError("down")

    transport(boom)

    result = await sms_api.send_sms(
        site_id="s", receiver_phone_number="01000000101", content="hi"
    )

    assert result == sms_api.SmsSendResult(success=False, message="ConnectError")


async def test_missing_base_url_skips_request(transport, monkeypatch):
    captured = transport(lambda _: httpx.Response(200, json={}))
    monkeypatch.setattr(sms_api, "RESERVATION_API_BASEURL", None)

    result = await sms_api.send_sms(
        site_id="s", receiver_phone_number="01000000101", content="hi"
    )

    assert result.success is False
    assert "request" not in captured


async def test_missing_receiver_skips_request(transport):
    captured = transport(lambda _: httpx.Response(200, json={}))

    result = await sms_api.send_sms(site_id="s", receiver_phone_number="", content="hi")

    assert result.success is False
    assert "request" not in captured


async def test_image_urls_go_into_request_json(transport):
    captured = transport(lambda _: httpx.Response(200, json={"sendStatus": "SUCCESS"}))

    await sms_api.send_sms(
        site_id="s",
        receiver_phone_number="01000000101",
        subject="[Example Device Care] 영업시간 안내",
        content="영업시간 안내",
        image_urls=["https://cdn.example.com/prd/public/a.jpg", "  ", ""],
    )

    payload, _ = _request_part(captured["request"])
    assert payload["imageUrls"] == ["https://cdn.example.com/prd/public/a.jpg"]


@pytest.mark.parametrize("image_urls", [None, [], ["", "  "]])
async def test_image_urls_omitted_when_empty(transport, image_urls):
    captured = transport(lambda _: httpx.Response(200, json={"sendStatus": "SUCCESS"}))

    await sms_api.send_sms(
        site_id="s",
        receiver_phone_number="01000000101",
        content="hi",
        image_urls=image_urls,
    )

    payload, _ = _request_part(captured["request"])
    assert "imageUrls" not in payload

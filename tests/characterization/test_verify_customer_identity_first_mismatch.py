from types import SimpleNamespace

from tasks.verify_customer_identity_task import VerifyCustomerIdentityTask


def _customer(
    no: str,
    verification_code: str,
    *,
    name: str = "Hong Gil-dong",
    phone: str = "01000000101",
) -> SimpleNamespace:
    return SimpleNamespace(
        customerNo=no,
        customerNm=name,
        verification_code=verification_code,
        telNum=phone,
    )


def _task(
    collected: list[str | None],
) -> tuple[VerifyCustomerIdentityTask, list[str]]:
    userdata = SimpleNamespace(
        room_name="room-test",
        recipient_data=SimpleNamespace(phone="01000000101"),
        caller_disconnected=False,
        call_config_data=SimpleNamespace(language="ko-KR"),
    )
    task = VerifyCustomerIdentityTask(userdata, action="inquiry")
    customers = [
        _customer("1", "314159"),
        _customer("2", "271828"),
    ]

    async def _lookup(_phone):
        return "01000000101", customers

    remaining = list(collected)
    collection_calls: list[str] = []

    async def _collect(purpose):
        collection_calls.append(purpose)
        return remaining.pop(0) if remaining else None

    task._lookup_customers_by_phone = _lookup
    task._collect_verification_code = _collect
    return task, collection_calls


async def test_first_confirmed_mismatch_escalates_without_second_collection():
    task, collection_calls = _task(["111111", "314159"])

    result = await task.run()

    assert result.status == "escalated"
    assert result.escalation_reason == "no_match"
    assert len(collection_calls) == 1


async def test_collection_failure_still_gets_second_chance_to_match():
    task, collection_calls = _task([None, "314159"])

    result = await task.run()

    assert result.status == "matched"
    assert result.customer.customerNo == "1"
    assert len(collection_calls) == 2


async def test_two_collection_failures_remain_verification_code_not_provided():
    task, collection_calls = _task([None, None])

    result = await task.run()

    assert result.status == "escalated"
    assert result.escalation_reason == "verification_code_not_provided"
    assert len(collection_calls) == 2


async def test_ambiguous_verification_code_selects_first_match_without_customer_number():
    task, collection_calls = _task(["314159"])
    first_match = _customer(
        "1",
        "314159",
        name="First Customer",
        phone="01000000102",
    )
    second_match = _customer(
        "2",
        "314159",
        name="Second Customer",
        phone="01000000103",
    )

    async def _lookup(_phone):
        return "01000000101", [first_match, second_match]

    task._lookup_customers_by_phone = _lookup
    task._userdata.recipient_data.customer_no = "stale-customer-no"

    result = await task.run()

    assert result.status == "matched"
    assert result.customer is first_match
    assert result.escalation_reason is None
    assert len(collection_calls) == 1
    assert task._userdata.recipient_data.phone == "01000000102"
    assert task._userdata.recipient_data.verification_code == "314159"
    assert task._userdata.recipient_data.user_name == "First Customer"
    assert task._userdata.recipient_data.customer_no is None


async def test_unique_verification_code_match_keeps_customer_number():
    task, _ = _task(["314159"])
    task._userdata.recipient_data.customer_no = "stale-customer-no"

    result = await task.run()

    assert result.status == "matched"
    assert result.customer.customerNo == "1"
    assert task._userdata.recipient_data.customer_no == "1"

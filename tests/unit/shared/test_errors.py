from typing import ClassVar

from merchant_assistant.shared.errors import DomainError


class OutOfStockError(DomainError):
    code: ClassVar[str] = "out_of_stock"
    user_message: ClassVar[str] = "That item is not available."


def test_the_public_view_has_a_stable_code_and_a_safe_message() -> None:
    error = OutOfStockError("sku=ZT-PRO-2 table=items row=17")

    assert error.public() == {"code": "out_of_stock", "message": "That item is not available."}


def test_the_public_view_never_carries_the_internal_detail() -> None:
    error = OutOfStockError("sku=ZT-PRO-2 table=items row=17")

    assert "ZT-PRO-2" not in str(error.public())


def test_the_detail_stays_available_for_the_logs() -> None:
    error = OutOfStockError("sku=ZT-PRO-2 table=items row=17")

    assert "sku=ZT-PRO-2" in str(error)
    assert error.detail == "sku=ZT-PRO-2 table=items row=17"


def test_without_a_detail_the_message_falls_back_to_the_code() -> None:
    assert str(OutOfStockError()) == "out_of_stock"

from typing import Optional

from pydantic import BaseModel, Field


class GetConfirmedReservationsRequest(BaseModel):
    site_id: str
    phone: str = Field(..., description="전화번호")
    customer_no: Optional[str] = Field(None, description="고객 번호")
    verification_code: Optional[str] = Field(None, description="데모 확인번호 (6자리 숫자)")


class GetCustomerRequest(BaseModel):
    site_id: str
    phone: str = Field(..., description="전화번호")

    # @field_validator("phone")
    # def validate_phone(cls, v):
    #     pattern = r"^0\d{8,10}$"
    #     if not re.match(pattern, v):
    #         raise ValueError("유효하지 않은 전화번호 형식입니다.")
    #     return v

from typing import Optional

from utils.i18n import t

_KOREAN_DIGITS = ("공", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구")


def build_verification_code_confirmation_announcement(
    digits: str, language: Optional[str] = None
) -> str:
    if not language or language == "ko-KR":
        spoken_digits = " ".join(
            "".join(_KOREAN_DIGITS[int(digit)] for digit in digits[index : index + 2])
            for index in range(0, 6, 2)
        )
        josa = "이" if digits[-1] in "013678" else "가"
    else:
        spoken_digits = ", ".join(
            " ".join(digits[index : index + 2]) for index in range(0, 6, 2)
        )
        josa = ""
    return t(
        "verification_code.confirm.announcement",
        language,
        digits=spoken_digits,
        josa=josa,
    )

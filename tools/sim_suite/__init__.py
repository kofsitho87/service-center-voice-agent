"""customer-sim 회귀 스위트 지원 패키지.

`tools/sim-suite.py`가 쓰는 스키마 로더, 이벤트 파서, 결정론적 체크,
LLM 저지, 러너, 리포터를 모아 둔다.

`tools/` 디렉터리를 sys.path에 넣어 두어 같은 디렉터리의 `customer_actor`
모듈(고객 LLM 공용 코드)을 어디서 import 하든 찾을 수 있게 한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = TOOLS_DIR.parent

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

__all__ = ["PROJECT_ROOT", "TOOLS_DIR"]

"""python -m stockout_alert 로 실행될 때의 진입점."""

import sys

if sys.version_info < (3, 10):  # 타입 표기 "X | None"이 3.10부터라서, 그 전 버전이면 알아듣기 쉬운 말로 끝낸다
    print(f"Python 3.10 이상이 필요합니다. 지금은 {sys.version.split()[0]}입니다.", file=sys.stderr)
    sys.exit(1)

from .cli import main  # noqa: E402  (버전 확인 뒤에 불러와야 3.9에서도 위 안내가 나온다)

if __name__ == "__main__":
    sys.exit(main())

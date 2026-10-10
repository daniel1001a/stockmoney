"""Fill docs/desk/MUSE_PROMPT.md with the real channel values from .env.

    .venv/bin/python scripts/muse_prompt_fill.py
    pbcopy < data/desk/MUSE_PROMPT_FILLED.md

The output lives under data/desk/ (gitignored). Nothing secret is printed.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "docs/desk/MUSE_PROMPT.md"
OUT = ROOT / "data/desk/MUSE_PROMPT_FILLED.md"


def load_env(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
            if m and m.group(2).strip():
                env.setdefault(m.group(1), m.group(2).strip())
    return env


def main() -> int:
    env = load_env(ROOT / ".env")
    values = {
        "<密鑰>": env.get("MUSE_SHARED_TOKEN", ""),
        "<DESK信箱>": env.get("DESK_MAIL_ADDRESS", ""),
        "<通知管道>": env.get("MUSE_CHANNEL", "Muse app 裡跟我的對話"),
    }
    missing = [k for k, v in values.items() if not v]
    if missing:
        print("缺少設定:", ", ".join(missing), file=sys.stderr)
        return 2
    text = TEMPLATE.read_text()
    start = text.index("`````\n") + len("`````\n")
    end = text.rindex("`````")
    body = text[start:end]
    for placeholder, value in values.items():
        body = body.replace(placeholder, value)
    leftover = re.findall(r"<(?:密鑰|DESK信箱|通知管道)>", body)
    if leftover:
        print("仍有未填的佔位符:", set(leftover), file=sys.stderr)
        return 3
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(body)
    OUT.chmod(0o600)
    print(f"已寫入 {OUT.relative_to(ROOT)}({len(body)} 字元)。複製:pbcopy < {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

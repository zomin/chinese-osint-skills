#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""
Run Fixtures — 跑 tests/fixtures/ 下的合成夹具，人工评审用。

不是 CI 门禁（仓库没有 CI，见 CONTRIBUTING §1）：它只做一件事，
把每个夹具喂给 ``scripts/`` 里对应的脚本，比对回报的 JSONL 与夹具里
声明的期望值，逐条打印通过/失败。

用法::

    python tests/run-fixtures.py                  # 全部夹具
    python tests/run-fixtures.py id_card_parse    # 只跑某个脚本的夹具
    python tests/run-fixtures.py phone_locate -v  # 打印每条记录

phone_locate 的夹具是**文本描述**而不是二进制：这里有 6 行代码按
``phone.dat`` 布局现场拼一个临时文件（头部 + 记录区 + 尾部索引），
所以仓库里不必存二进制，夹具本身也完全可读、可审查。

退出码：0 全通过 / 1 有失败 / 2 没有夹具或脚本缺失。

MIT License.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

SCRIPTS_BY_DIR = {
    "id_card_parse": "id_card_parse.py",
    "phone_locate": "phone_locate.py",
}


# --------------------------------------------------------------------------- 比对

def _resolve(record: dict[str, Any], dotted: str) -> Any:
    """``address.candidates`` 这种点路径取值；取不到返回 ``KeyError`` 哨兵。"""
    current: Any = record
    for part in dotted.split("."):
        if part == "names" and isinstance(current, dict) and "candidates" in current:
            return [c.get("name") for c in current["candidates"]]
        if part == "province" and isinstance(current, dict) and "resolved" in current:
            return (current["resolved"] or {}).get("province")
        if part == "city" and isinstance(current, dict) and "resolved" in current:
            return (current["resolved"] or {}).get("city")
        if part == "county" and isinstance(current, dict) and "resolved" in current:
            return (current["resolved"] or {}).get("county")
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def _compare(record: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key, want in expect.items():
        got = _resolve(record, key)
        if got is _MISSING:
            problems.append(f"{key}: 记录里没有这个字段")
        elif got != want:
            problems.append(f"{key}: 期望 {want!r}，实际 {got!r}")
    return problems


# --------------------------------------------------------------------------- phone.dat 构造

def build_phone_dat(spec: dict[str, Any]) -> bytes:
    header_size, entry_size = 8, 9
    encoding = spec.get("encoding", "utf-8")
    version = (spec.get("version") or "0000").encode("ascii")[:4].ljust(4, b"0")

    records_blob = bytearray()
    index: list[tuple[int, int, int]] = []
    for entry in spec.get("entries", []):
        payload = entry["record"].encode(encoding)
        offset = header_size + len(records_blob)
        records_blob += payload
        index.append((int(entry["prefix"]), offset, len(payload)))
    index.sort()

    body = bytearray()
    for prefix, offset, length in index:
        body += struct.pack("<IIB", prefix, offset, length)

    index_offset = header_size + len(records_blob)
    data = struct.pack("<I4s", index_offset, version) + bytes(records_blob) + bytes(body)
    cut = int(spec.get("truncateBytes") or 0)
    return data[:-cut] if cut else data


# --------------------------------------------------------------------------- 单个夹具

def run_fixture(script: str, fixture: Path, verbose: bool) -> tuple[bool, list[str]]:
    spec = json.loads(fixture.read_text(encoding="utf-8"))
    script_path = SCRIPTS / script
    if not script_path.is_file():
        return False, [f"找不到脚本 {script_path}"]

    with tempfile.TemporaryDirectory() as tmp:
        args = [sys.executable, str(script_path), *spec.get("args", [])]
        queries: list[str] = spec.get("queries") or spec.get("ids") or []
        if "dat" in spec:
            db_path = Path(tmp) / "phone.dat"
            db_path.write_bytes(build_phone_dat(spec["dat"]))
            args += ["--db", str(db_path)]
        stdin = "\n".join(queries) + "\n"
        proc = subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            # 脚本自身把 stdout 钉成 UTF-8；这里再兜一层，避免某台机器上出现
            # 一个非 UTF-8 字节就让整个 runner 以 UnicodeDecodeError 崩掉。
            errors="replace",
            cwd=tmp,
        )

    problems: list[str] = []
    want_exit = spec.get("expect_exit")
    if want_exit is not None and proc.returncode != want_exit:
        problems.append(f"退出码 期望 {want_exit}，实际 {proc.returncode}；stderr={proc.stderr.strip()}")

    records: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            problems.append(f"输出不是合法 JSONL：{exc}：{line[:120]}")
            break

    expects = spec.get("expect") or []
    if len(records) != len(expects):
        problems.append(f"记录条数 期望 {len(expects)}，实际 {len(records)}")
    for i, expect in enumerate(expects):
        if i >= len(records):
            break
        problems.extend(f"[{i}] {msg}" for msg in _compare(records[i], expect))
    if verbose:
        for record in records:
            print("      ", json.dumps(record, ensure_ascii=False)[:200])
    return (not problems), problems


# --------------------------------------------------------------------------- 主流程

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "-v" in argv or "--verbose" in argv
    names = [a for a in argv if not a.startswith("-")]

    targets = [
        (script, FIXTURES / folder)
        for folder, script in SCRIPTS_BY_DIR.items()
        if not names or folder in names or script in names
    ]
    if not targets:
        print(f"没有匹配的夹具（可选 {sorted(SCRIPTS_BY_DIR)}）", file=sys.stderr)
        return 2

    total = failed = 0
    for script, folder in targets:
        if not folder.is_dir():
            print(f"跳过：{folder} 不存在", file=sys.stderr)
            continue
        print(f"\n== {script}")
        for fixture in sorted(folder.glob("*.json")):
            total += 1
            ok, problems = run_fixture(script, fixture, verbose)
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {fixture.stem}")
            if not ok:
                failed += 1
                for problem in problems:
                    print(f"         - {problem}")

    if total == 0:
        print("没有找到任何夹具", file=sys.stderr)
        return 2
    print(f"\n{total - failed}/{total} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

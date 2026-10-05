"""配置契约守卫：`.env.example` 和代码里的配置字段必须是同一份真相。

为什么需要这个文件：撤下语音识别时，`app/config.py` 里的 `asr_*` / `whisper_*` /
`vad_*` / `audio_*` 字段全都删干净了，`backend/tests` 也全绿 ——
**但 `.env.example` 把十四个已经失效的键继续留在那里**，还配着
「引擎：mock | cloud | local」这样的注释。

后果不是崩溃，而是**误导**：照着示例文件配环境的人会以为还有语音功能可用，
填了 `ASR_BASE_URL` 之后既不报错也不生效 —— 又一个「静默失效」。

这类 bug 的共性是：`.env.example` 是个纯文本文件，谁都不 import 它，
所以删字段时没人会想起来它。能自动抓住它的只有「拿代码里的字段当基准去比对」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
EXAMPLE = PROJECT / ".env.example"

sys.path.insert(0, str(BACKEND))

try:  # pytest 可选：没有 pytest 时用本文件的 main() 直跑
    import pytest
except ImportError:  # pragma: no cover
    pytest = None


class _SkipTest(Exception):
    pass


def _skip(reason: str) -> None:
    if pytest is not None and os.environ.get("PYTEST_CURRENT_TEST"):
        pytest.skip(reason)
    raise _SkipTest(reason)


def _parse_example_keys(text: str) -> list[str]:
    """取出示例文件里所有生效的 KEY=... 行（跳过注释与空行）。"""
    keys: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.append(line.split("=", 1)[0].strip())
    return keys


def _example_keys() -> list[str]:
    assert EXAMPLE.is_file(), f"找不到示例配置：{EXAMPLE}"
    return _parse_example_keys(EXAMPLE.read_text(encoding="utf-8"))


def _settings_fields() -> set[str]:
    """Settings 的字段名集合（小写），即 cfg() 的键名。"""
    try:
        from app.config import Settings
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过配置契约用例：{exc}")
        raise AssertionError("unreachable") from exc
    return set(Settings.model_fields)


def test_example_has_no_key_that_code_does_not_know() -> None:
    """`.env.example` 里不许出现 `Settings` 不认识的键。

    这是本文件存在的理由：删掉一个配置项时，最容易被漏掉的就是示例文件。
    键名大小写不敏感（Settings 配了 case_sensitive=False），所以统一小写比较。
    """
    known = _settings_fields()
    unknown = sorted(k for k in _example_keys() if k.lower() not in known)
    assert not unknown, (
        f".env.example 里有 {len(unknown)} 个代码里已不存在的配置键：{unknown}。"
        "删配置项时请同步删掉示例文件里对应的行 —— "
        "否则照示例配置的人会填了个「不报错也不生效」的值。"
    )


def test_every_field_is_documented_in_example() -> None:
    """反过来也要成立：每个配置字段都该在示例文件里有位置。

    `.env.example` 是用户唯一的配置参考。漏掉一个字段，用户就只能去读源码 ——
    而这个项目的目标用户不是开发者。
    """
    documented = {k.lower() for k in _example_keys()}
    missing = sorted(f for f in _settings_fields() if f not in documented)
    assert not missing, f"这些配置字段没有写进 .env.example：{missing}"


def test_no_duplicate_keys_in_example() -> None:
    """重复的键会让「以哪一行为准」变成实现细节，读的人无法判断。"""
    keys = [k.lower() for k in _example_keys()]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    assert not dup, f".env.example 里有重复的键：{dup}"


def test_editable_and_secret_keys_are_real_fields() -> None:
    """`EDITABLE_KEYS` / `SECRET_KEYS` 是白名单，指向不存在的字段就是死名单。

    这两个常量决定「前端能改哪些」和「哪些要打码」，漂移的后果分别是
    静默无效的保存、以及该打码的没打码。
    """
    try:
        from app.config import EDITABLE_KEYS, SECRET_KEYS
    except ImportError as exc:  # pragma: no cover
        _skip(f"缺少后端依赖，跳过配置契约用例：{exc}")
        raise AssertionError("unreachable") from exc

    known = _settings_fields()
    for name, keys in (("EDITABLE_KEYS", EDITABLE_KEYS), ("SECRET_KEYS", SECRET_KEYS)):
        stale = sorted(k for k in keys if k not in known)
        assert not stale, f"{name} 里指向了不存在的配置字段：{stale}"

    # 密钥类字段必须真的在 SECRET_KEYS 里，否则 /api/health/details 会漏出明文
    for key in ("llm_api_key", "embed_api_key"):
        if key in known:
            assert key in SECRET_KEYS, f"{key} 没进 SECRET_KEYS，健康接口会泄漏明文密钥"


# ---------------------------------------------------------------- 直跑入口


def _collect_tests() -> list:
    return [(name, obj) for name, obj in list(globals().items())
            if name.startswith("test_") and callable(obj)]


def main() -> int:
    print("=" * 68, flush=True)
    print("配置契约回归：.env.example 与 Settings 字段必须一致", flush=True)
    print("=" * 68, flush=True)
    failures: list[tuple[str, str]] = []
    skipped = 0
    tests = _collect_tests()
    for index, (name, function) in enumerate(tests, start=1):
        try:
            function()
        except _SkipTest as exc:
            skipped += 1
            print(f"[{index:>2}/{len(tests)}] SKIP {name} —— {exc}", flush=True)
            continue
        except Exception as exc:  # noqa: BLE001
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"[{index:>2}/{len(tests)}] FAIL {name} —— {type(exc).__name__}: {exc}", flush=True)
            continue
        print(f"[{index:>2}/{len(tests)}] OK   {name}", flush=True)
    print("-" * 68, flush=True)
    print(f"通过 {len(tests) - len(failures) - skipped} / 跳过 {skipped} / 失败 {len(failures)}", flush=True)
    if failures:
        for name, error in failures:
            print(f"  FAILED {name}: {error}", flush=True)
        print("结果：失败", flush=True)
        return 1
    print("结果：全部通过。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

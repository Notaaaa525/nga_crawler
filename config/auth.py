"""登录凭据加载。

优先级（来源组优先；同组内完整 Cookie 优先于 UID + CID）：

1. 环境变量 ``NGA_COOKIE``
2. 环境变量 ``NGA_UID`` + ``NGA_CID``
3. ``.env`` 中的 ``NGA_COOKIE``
4. ``.env`` 中的 ``NGA_UID`` + ``NGA_CID``

不允许跨来源拼接（如 env 只给 UID、``.env`` 只给 CID），也不回退到硬编码 Cookie。
日志中只允许输出 ``Auth.source``；如需调试请使用 ``log.mask_secret``。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = BASE_DIR / ".env"


class AuthConfigError(RuntimeError):
    """凭据缺失或格式非法。"""


@dataclass(frozen=True)
class Auth:
    """一次运行使用的登录凭据；``source`` 只记录来源，不记录值。"""

    cookie: str
    uid: Optional[str]
    cid: Optional[str]
    source: str


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def parse_env_file(path: Path) -> Dict[str, str]:
    """解析 ``.env`` 文件（标准库实现，不引入 dotenv）。

    - 编码 ``utf-8-sig``；忽略空行与 ``#`` 开头的行；
    - 支持可选 ``export `` 前缀；
    - 以第一个 ``=`` 分割；值两端空白去除，成对单/双引号去除；
    - 行内 ``#`` 不视为注释（Cookie 可能含特殊字符）；
    - 重复键后者覆盖前者。
    """
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return values
    except OSError as exc:  # 存在但不可读：配置错误，不静默忽略
        raise AuthConfigError(f"无法读取凭据文件 {path}：{exc}") from exc

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
            if not line:
                continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _from_cookie(cookie: str, source: str) -> Auth:
    lowered = cookie.lower()
    if "ngapassportuid=" not in lowered or "ngapassportcid=" not in lowered:
        raise AuthConfigError(
            "Cookie 缺少 ngaPassportUid / ngaPassportCid，请重新获取完整 Cookie "
            "并更新 .env 中的 NGA_COOKIE（或 NGA_UID/NGA_CID）"
        )
    return Auth(cookie=cookie, uid=None, cid=None, source=source)


def _from_parts(uid: str, cid: str, source: str) -> Auth:
    cookie = f"ngaPassportUid={uid}; ngaPassportCid={cid}"
    return Auth(cookie=cookie, uid=uid, cid=cid, source=source)


def load_auth(env_path: Optional[Path] = None) -> Auth:
    """按优先级加载凭据；全部缺失时抛 :class:`AuthConfigError`。"""
    path = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    file_values = parse_env_file(path)

    env_cookie = _clean(os.environ.get("NGA_COOKIE"))
    env_uid = _clean(os.environ.get("NGA_UID"))
    env_cid = _clean(os.environ.get("NGA_CID"))
    file_cookie = _clean(file_values.get("NGA_COOKIE"))
    file_uid = _clean(file_values.get("NGA_UID"))
    file_cid = _clean(file_values.get("NGA_CID"))

    if env_cookie:
        return _from_cookie(env_cookie, "env:NGA_COOKIE")
    if env_uid and env_cid:
        return _from_parts(env_uid, env_cid, "env:uid+cid")
    if file_cookie:
        return _from_cookie(file_cookie, ".env:NGA_COOKIE")
    if file_uid and file_cid:
        return _from_parts(file_uid, file_cid, ".env:uid+cid")

    raise AuthConfigError(
        "未找到有效凭据，请任选一种方式配置：\n"
        "  1) 设置环境变量 NGA_COOKIE=ngaPassportUid=...; ngaPassportCid=...\n"
        "  2) 设置环境变量 NGA_UID=... 与 NGA_CID=...\n"
        f"  3) 复制 .env.example 为 {path}，填入上述任一方式\n"
        "获取 Cookie：浏览器登录 https://bbs.nga.cn/ → F12 Console → 输入 document.cookie"
    )

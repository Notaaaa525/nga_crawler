# NGA 特化爬虫（nga_crawler）

> 99%基于DeepseekHarness开发。
> 面向 NGA 单帖的**两阶段爬虫**：先按页抓取并缓存原始 JSON，再离线解析、导出可阅读的 TXT。
> 仅依赖 Python 3.8+ 标准库，无需安装任何第三方包。

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![Tests](https://img.shields.io/badge/tests-183%20passing-brightgreen)
![Platform](https://img.shields.io/badge/platform-cross--platform-lightgrey)
![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/license-CC%20BY--NC--SA%204.0-lightgrey)

## 简介

NGA 的帖子页接口在带 `__output=11` 时返回结构化 JSON。本项目把「抓取」和「解析」彻底拆开：

- **crawl**：只负责请求、分页判断、限速重试与按页落盘（`cache/{tid}/page_XXX.json`）；
- **parse**：只读本地缓存，不触网、不读凭据，可随时重复生成 `output/{tid}.txt`。

解析规则变化时只需重跑 `parse`，不必重新抓取；抓取失败按页隔离，可断点续跑。

## 特性

- **按页缓存、宽进不裁剪**：成功页保留原始 `data`，将来新增字段无需重爬；
- **断点续跑**：`state.json` 记录 `last_page` 与 `failed_pages`，默认从最早失败页或下一页继续；
- **失败域隔离**：认证失效、限流、网络错误、永久主题错误分类处理，单页失败不影响其他页；
- **限速与退避**：页间固定间隔 + 429 单次退避 + 连续限流中止；
- **纯离线解析**：标题、作者、楼层、时间、正文、引用、图片链接、编辑标记、缺失字段占位；
- **可读 TXT**：固定文件头、楼层分隔线、引用 `>` 前缀、`[缺失: 字段]` 占位，UTF-8 + LF；
- **可追溯日志**：轮转 5MB × 3，`req_id` 贯穿缓存与日志，Cookie 只记录来源不记录值；
- **两种入口**：完整 CLI，以及 Windows 快捷菜单 `run.bat`；
- **离线测试**：183 个单元测试，含架构纪律检查（nga_parser/render 不触网、不读写文件）。

## 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.8 及以上 |
| 依赖 | 无（仅标准库） |
| 操作系统 | Windows / Linux / macOS（`run.bat` 仅 Windows） |
| 凭据 | 有效的 NGA 登录 Cookie |

## 快速开始

### 1. 获取代码

```bash
git clone https://github.com/Notaaaa525/nga_crawler.git
cd nga_crawler
```

### 2. 配置凭据（二选一）

```bash
cp .env.example .env
# Windows: copy .env.example .env
```

编辑 `.env`，任选一种方式：

```dotenv
# 方式一：完整 Cookie（推荐）
NGA_COOKIE=ngaPassportUid=...; ngaPassportCid=...

# 方式二：UID + CID
NGA_UID=...
NGA_CID=...
```

也可以直接设置同名**环境变量**（优先于 `.env`）。

> Cookie 获取：浏览器登录 <https://bbs.nga.cn/> → F12 → Console → 输入 `document.cookie` 回车，复制整行。

### 3. 抓取帖子页

```bash
# 抓取第 1-20 页
python main.py crawl --tid <tid> --pages 1-20

# 从第 21 页抓到结尾
python main.py crawl --tid <tid> --pages 21-

# 默认续跑（最早失败页或 last_page+1）
python main.py crawl --tid <tid>

# 忽略缓存强制重抓
python main.py crawl --tid <tid> --force
```

### 4. 离线解析并生成 TXT

```bash
# 清空重建输出
python main.py parse --tid <tid> --format txt --force

# 只解析第 1-10 页
python main.py parse --tid <tid> --pages 1-10
```

输出文件：`output/<tid>.txt`。

### 5. Windows 快捷菜单

直接双击 `run.bat`（或在项目目录执行），用键盘选择操作：

> `run.bat` 只做「探测 Python → 启动 `menu.py`」；菜单文案与操作逻辑都在 Python 中，
> 避免 `cmd.exe` 解析 UTF-8 批处理时对中文的截断问题（旧版曾出现菜单行被当成命令执行）。

| 编号 | 功能 | 编号 | 功能 |
|---|---|---|---|
| 1 | 抓取整帖 | 8 | 查看版本 |
| 2 | 抓取指定页码范围 | 9 | 检查/配置凭据 |
| 3 | 默认续跑 | A | 打开输出目录 |
| 4 | 强制重抓 | S | 查看 `state.json` 进度 |
| 5 | 解析并重建 TXT | T | 运行单元测试 |
| 6 | 解析指定页码范围 | R | 打开 README |
| 7 | 查看帮助 | 0 | 退出 |

菜单只调用 `python main.py ...`，不写入任何凭据；每次执行后显示退出码含义。

## 命令行参考

### `crawl`

| 参数 | 默认 | 说明 |
|---|---|---|
| `--tid TID` | `config/targets.py` 的 `DEFAULT_TID` | 帖子 ID（正整数） |
| `--pages SPEC` | 续跑 | `all` / `N` / `A-B` / `A-`；省略时从最早失败页或 `last_page+1` 开始 |
| `--max-pages N` | `0` | 本次最多抓多少页（0=不限制） |
| `--force` | 关 | 忽略缓存重抓；不重置 state；省略 `--pages` 时等价 `all` |
| `--delay SEC` | `2.0` | 页间固定间隔秒数 |
| `--retries N` | `3` | 网络/5xx 重试次数（线性退避 2/4/6 秒） |
| `--backoff-429 SEC` | `15` | 429 退避基数（另加 0-3 秒抖动） |
| `--timeout SEC` | `30` | 单次请求超时 |
| `--fid` / `--min-replies` | `0` | 列表页预留参数，MVP 不影响抓取 |
| `--cache-dir DIR` | `cache` | 缓存根目录 |

### `parse`

| 参数 | 默认 | 说明 |
|---|---|---|
| `--tid TID` | 同 crawl | 帖子 ID |
| `--pages SPEC` | `all` | `all` / `N` / `A-B` / `A-`；开区间终点取最大缓存页 |
| `--format` | `txt` | MVP 仅支持 `txt` |
| `--force` | 关 | 清空并重建输出（推荐） |
| `--cache-dir DIR` | `cache` | 缓存根目录 |

通用：`-h/--help` 查看帮助与示例，`-V/--version` 查看版本；`--log-level`、`--log-path` 控制日志。

## 配置

| 类别 | 优先级 |
|---|---|
| 运行参数 | CLI > 环境变量 > 默认值 |
| 凭据 | 环境变量 > `.env` > 明确报错 |

可用环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `NGA_COOKIE` | — | 完整 Cookie |
| `NGA_UID` / `NGA_CID` | — | UID + CID 拼接 Cookie |
| `NGA_DELAY_SECONDS` | `2.0` | 页间间隔 |
| `NGA_MAX_RETRIES` | `3` | 网络/5xx 重试次数 |
| `NGA_BACKOFF_429_SECONDS` | `15` | 429 退避基数 |
| `NGA_TIMEOUT` | `30` | 请求超时 |
| `NGA_CACHE_DIR` | `cache` | 缓存目录 |
| `NGA_LOG_LEVEL` | `INFO` | 日志级别 |
| `NGA_LOG_PATH` | `logs/nga.log` | 日志文件 |

`.env` 已加入 `.gitignore`，不会被提交；默认目标 tid 可在 `config/targets.py` 修改。

## TXT 输出

```text
================================================================================
NGA 帖子导出
TID: <tid>
标题: 示例帖子标题
楼主: 示例楼主
总页数: 2    本次收录页: 1-2
抓取时间: 2026-01-01 12:00:00 ~ 2026-01-01 12:05:00
parser_version: 1.0.0    fetcher_version: 1.0.0
收录楼层: 40    字段缺失: 0 处
================================================================================

===== 第 0 楼 | 示例楼主 | 2026-01-01 11:00 =====
作者: 示例楼主（楼主）
时间: 2026-01-01 11:00

正文:
正文第一段……

> 引用内容第一行
> 引用内容第二行

[图片: https://img.nga.cn/attachments/mon_202601/01/example.jpg]

[编辑] 本帖最后由 示例楼主 于 2026-01-01 11:05 编辑
```

- 每页缓存、逐页流式写出，不一次性把整帖读入内存；
- 图片只输出绝对 URL，不下载文件；
- 缺失字段用 `[缺失: 字段名]` 占位，不静默丢弃；
- 不在 TXT 中输出 `req_id` / `pid`。

## 缓存、状态与断点续跑

```text
cache/{tid}/page_001.json      # 单页缓存（外层元数据 + 原始 JSON）
state.json                     # 全局进度（按 tid 分组）
output/{tid}.txt               # 解析输出
logs/nga.log                   # 日志（轮转 5MB × 3）
```

- 缓存外层字段：`tid/page/url/fetched_at/status/req_id/page_type/total_pages/current_page/next_url/fetcher_version`；
- 成功页 `status="ok"`、`page_type="thread"`，内层 `content.data` 为 NGA 原始 JSON；
- 错误页写「标记缓存」（`status != "ok"`），只保留诊断摘要，永不当作成功页复用；
- 只有成功页能被跳过；文件损坏或状态不符时按缺失重抓；
- 部分页失败时退出码为 2，但 `state.json` 已保存，重跑会自动补洞。

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 成功（crawl 区间全部成功或空区间；parse 至少解析 1 页） |
| `1` | 参数错误（CLI 语法、页码区间、格式等） |
| `2` | 运行时错误（凭据失效、连续限流、永久错误、部分页失败、写盘失败等） |
| `130` | 用户中断（crawl 已保存 state；parse 已清理临时文件） |

## 项目结构

```text
nga_crawler/
├── main.py                 # 入口
├── cli.py                  # argparse / 子命令 / 退出码
├── engine.py               # crawl / parse 两个循环、区间、state、输出编排
├── crawler.py              # 请求、分类、重试、原子写缓存（唯一触网模块）
├── nga_parser.py           # 纯函数：cache → ParsedPage（避免与 3.8/3.9 内置 parser 冲突）
├── render.py               # 纯函数：ParsedPage → TXT
├── log.py                  # 日志轮转、req_id、脱敏
├── run.bat                 # Windows 启动器（探测 Python 后运行 menu.py）
├── menu.py                 # 交互菜单（中文菜单与操作逻辑）
├── config/                 # 凭据 / 目标 / 运行参数
├── tests/                  # 离线单元测试与 fixtures
├── docs/                   # 维护记录
├── LICENSE                 # CC BY-NC-SA 4.0 许可
├── .env.example
├── .gitignore
├── .gitattributes
└── requirements.txt
```

架构约束：依赖方向只能是 `main → cli → engine → crawler/nga_parser/render/config/log`；
`crawler` 是唯一直接使用 `urllib` 的模块，`nga_parser`/`render` 不触网、不读写文件（由 AST 测试守护）；
缓存、状态与输出均采用「同目录临时文件 + `os.replace`」原子写。

> 命名说明：解析模块文件名为 `nga_parser.py`。Python 3.8/3.9 自带同名内置模块 `parser`，
> 会优先于项目目录中的 `parser.py` 被导入，改名后可在 3.8+ 正常工作（CLI 子命令仍是 `parse`）。

## 测试

```bash
cd nga_crawler
python -m unittest discover -s tests -t .
```

- 仅使用标准库 `unittest`，全部离线、确定性、使用临时目录；
- 覆盖 nga_parser/render/crawler/engine/cli，以及架构 import 纪律；
- `docs/cli-ux-improvements.md` 记录了 CLI 交互修复的问题清单与验收。

## 常见问题

**Q：Cookie 失效怎么办？**
`crawl` 会立即以退出码 2 结束并提示更新 `.env`；更新 `NGA_COOKIE` 后重跑即可，默认会从失败页继续。

**Q：遇到 429 / 刷新过快？**
会退避重试一次；连续多页限流会中止本次运行。可增大 `--delay`、稍后再试；state 已保存，不会丢进度。

**Q：部分页失败会不会影响其他页？**
不会。失败页记入 `state.json` 的 `failed_pages`，其他页继续；重跑时默认从最早失败页补抓。

**Q：可以离线重新生成 TXT 吗？**
可以。`parse` 只读 `cache/`，不需要 Cookie、不联网；调整 `--pages` 即可分段输出。

**Q：图片会被下载吗？**
不会，只保留 `img.nga.cn` 绝对链接。

**Q：能一次抓多个帖子吗？**
当前一次一个 tid。批量可循环调用（缓存按 tid 隔离）。

**Q：抓大帖子很慢？**
NGA 每页 20 楼，默认 2 秒/页；可用 `--max-pages` 控量、分多次抓取，缓存会沿用。

## 安全与合规

- 本项目仅用于个人学习与内容存档，请遵守 NGA 用户协议及当地法律法规；
- 默认固定限速、单线程顺序抓取，不绕过登录或权限，不做高频请求；
- Cookie 只存在于内存与请求头，不写入缓存/TXT/日志；日志仅记录凭据来源；
- 不下载图片/附件，只保留 URL；
- 请勿公开传播抓取的帖子内容，尊重作者与站点权益。

## 贡献

欢迎 Issue / PR。提交前请：

1. 运行 `python -m unittest discover -s tests -t .` 并保证全绿；
2. 保持既有边界：`crawler` 不做业务字段提取，`nga_parser`/`render` 不触网、不读写文件；
3. 若调整站点字段解析，请同步更新 `tests/fixtures/` 与测试。

## 许可证

本项目采用 [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International（CC BY-NC-SA 4.0）](https://creativecommons.org/licenses/by-nc-sa/4.0/) 协议，完整法律文本见 [LICENSE](LICENSE)。

- **BY（署名）**：复制、分发或改编时须注明原作者与来源；
- **NC（非商业）**：不得将本项目或其衍生作品用于商业目的；
- **SA（相同方式共享）**：改编后的作品须以相同协议发布。

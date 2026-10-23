@echo off
rem ============================================================
rem  NGA 特化爬虫 - Windows 快捷菜单
rem  用法：双击本文件，或在 nga_crawler 目录执行 run.bat
rem  说明：只调用 python main.py，不写入任何凭据。
rem ============================================================
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
setlocal EnableExtensions EnableDelayedExpansion
title NGA 特化爬虫 - 快捷菜单
cd /d "%~dp0"

rem ---- 探测 Python ----
set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo.
    echo [错误] 未检测到 Python。请安装 Python 3.8 或更高版本，并加入 PATH。
    echo        下载地址：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [警告] 当前 Python 版本低于 3.8，程序可能无法正常运行。
    echo.
    pause
)

:menu
cls
echo ============================================================
echo    NGA 特化爬虫 - 快捷菜单
echo ============================================================
echo    [1] 抓取整帖（全部页）
echo    [2] 抓取指定页码范围
echo    [3] 默认续跑（补抓失败页）
echo    [4] 强制重抓（忽略缓存）
echo    [5] 解析并重建 TXT
echo    [6] 解析指定页码范围
echo    [7] 查看帮助
echo    [8] 查看版本
echo    [9] 检查/配置凭据
echo    [A] 打开输出目录
echo    [S] 查看进度状态
echo    [T] 运行单元测试
echo    [R] 打开 README
echo    [0] 退出
echo ------------------------------------------------------------
echo    退出码：0 成功 / 1 参数错误 / 2 运行时错误 / 130 用户中断
echo    首次使用请先执行 [9] 配置凭据；.env 不会被提交到仓库。
echo ============================================================
echo.
choice /c 1234567890ASTR /n /m "请输入菜单编号: " 2>nul
set "MENU_CODE=!ERRORLEVEL!"
if !MENU_CODE! equ 255 goto :no_console
if !MENU_CODE! equ 1 goto :crawl_all
if !MENU_CODE! equ 2 goto :crawl_pages
if !MENU_CODE! equ 3 goto :crawl_resume
if !MENU_CODE! equ 4 goto :crawl_force
if !MENU_CODE! equ 5 goto :parse_all
if !MENU_CODE! equ 6 goto :parse_pages
if !MENU_CODE! equ 7 goto :help
if !MENU_CODE! equ 8 goto :version
if !MENU_CODE! equ 9 goto :credentials
if !MENU_CODE! equ 10 goto :bye
if !MENU_CODE! equ 11 goto :open_output
if !MENU_CODE! equ 12 goto :show_state
if !MENU_CODE! equ 13 goto :run_tests
if !MENU_CODE! equ 14 goto :open_readme
echo.
echo [提示] 无效的菜单编号（!MENU_CODE!），请重新选择。
pause >nul
goto :menu

rem ------------------------------------------------------------
rem 子流程
rem ------------------------------------------------------------

:ask_tid
set "TID_ARG="
set "TID="
set /p "TID=请输入 tid（直接回车使用 config/targets.py 的默认值）："
if defined TID set "TID_ARG=--tid !TID!"
exit /b 0

:crawl_all
call :ask_tid
echo.
echo ^>^> %PY% main.py crawl !TID_ARG! --pages all
%PY% main.py crawl !TID_ARG! --pages all
set "RC=!ERRORLEVEL!"
call :show_result
goto :menu

:crawl_pages
call :ask_tid
set "PAGES="
set /p "PAGES=请输入页码范围（如 1-20 / 21- / all，直接回车=all）："
if not defined PAGES set "PAGES=all"
echo.
%PY% main.py crawl !TID_ARG! --pages "!PAGES!"
set "RC=!ERRORLEVEL!"
call :show_result
goto :menu

:crawl_resume
call :ask_tid
echo.
%PY% main.py crawl !TID_ARG!
set "RC=!ERRORLEVEL!"
call :show_result
goto :menu

:crawl_force
call :ask_tid
echo.
%PY% main.py crawl !TID_ARG! --force
set "RC=!ERRORLEVEL!"
call :show_result
goto :menu

:parse_all
call :ask_tid
echo.
%PY% main.py parse !TID_ARG! --format txt --force
set "RC=!ERRORLEVEL!"
call :show_result
goto :menu

:parse_pages
call :ask_tid
set "PAGES="
set /p "PAGES=请输入解析页面范围（如 1-10 / all，直接回车=all）："
if not defined PAGES set "PAGES=all"
echo.
%PY% main.py parse !TID_ARG! --pages "!PAGES!" --format txt --force
set "RC=!ERRORLEVEL!"
call :show_result
goto :menu

:help
cls
echo ============================================================
echo    查看帮助
echo ============================================================
echo    [1] 顶层帮助（示例 / 凭据 / 退出码）
echo    [2] crawl 帮助（抓取参数）
echo    [3] parse 帮助（解析参数）
echo    [0] 返回主菜单
echo ============================================================
echo.
choice /c 1230 /n /m "请选择: " 2>nul
set "HELP_CODE=!ERRORLEVEL!"
if !HELP_CODE! equ 255 goto :no_console
if !HELP_CODE! equ 4 goto :menu
if !HELP_CODE! equ 1 %PY% main.py -h
if !HELP_CODE! equ 2 %PY% main.py crawl -h
if !HELP_CODE! equ 3 %PY% main.py parse -h
set "RC=0"
call :show_result
goto :menu

:version
echo.
%PY% main.py -V
set "RC=!ERRORLEVEL!"
call :show_result
goto :menu

:credentials
cls
echo ============================================================
echo    凭据配置检查（只显示是否设置，不显示具体值）
echo ============================================================
if exist ".env" (
    echo    .env 文件：已存在
) else (
    echo    .env 文件：未找到
)
if defined NGA_COOKIE (
    echo    环境变量 NGA_COOKIE：已设置
) else (
    echo    环境变量 NGA_COOKIE：未设置
)
if defined NGA_UID (
    echo    环境变量 NGA_UID：已设置
) else (
    echo    环境变量 NGA_UID：未设置
)
if defined NGA_CID (
    echo    环境变量 NGA_CID：已设置
) else (
    echo    环境变量 NGA_CID：未设置
)
echo.
if not exist ".env" (
    choice /c YN /n /m "是否从 .env.example 创建 .env？[Y/N]: " 2>nul
    set "MK_CODE=!ERRORLEVEL!"
    if !MK_CODE! equ 255 goto :no_console
    if !MK_CODE! equ 1 (
        copy /y ".env.example" ".env" >nul
        echo.
        echo    已创建 .env。请用记事本填入 NGA_COOKIE，或 NGA_UID + NGA_CID。
    ) else (
        echo.
        echo    已取消创建。也可以直接设置同名环境变量。
    )
) else (
    echo    请确认 .env 中 NGA_COOKIE（或 NGA_UID + NGA_CID）已有值。
)
echo.
echo    提示：可用 [2] 抓 1 页快速验证凭据是否有效；失效会立即退出 2。
echo    .env 已加入 .gitignore，不会提交到仓库。
set "RC=0"
call :show_result
goto :menu

:show_state
cls
echo ============================================================
echo    抓取进度（state.json）
echo ============================================================
if exist "state.json" (
    type "state.json"
) else (
    echo    尚无 state.json：还没有成功抓取过，或尚未运行 crawl。
)
set "RC=0"
call :show_result
goto :menu

:open_output
if not exist "output" mkdir "output"
start "" "%~dp0output"
goto :menu

:run_tests
echo.
echo ^>^> %PY% -m unittest discover -s tests -t .
%PY% -m unittest discover -s tests -t .
set "RC=!ERRORLEVEL!"
call :show_result
goto :menu

:open_readme
start "" "%~dp0README.md"
goto :menu

:show_result
echo.
echo ------------------------------------------------------------
echo    退出码：!RC!  （0=成功 / 1=参数错误 / 2=运行时错误 / 130=用户中断）
echo ------------------------------------------------------------
pause
exit /b 0

:no_console
echo.
echo [错误] 当前环境不支持交互输入（标准输入不是控制台）。
echo        请直接双击 run.bat，或在 Windows 控制台执行；也可使用 python main.py 命令。
endlocal
exit /b 1

:bye
echo.
echo 已退出。祝抓取顺利。
endlocal
exit /b 0

# -*- coding: utf-8 -*-
"""Генерация запускных .bat (CP866, CRLF) БЕЗ backslash-эскейпов в путях.

Урок v0.42.0: «\\venv» в строке, прошедшей через слой эскейпов, превратился в
0x0B (вертикальная табуляция) — .bat уехал в релиз битым, запуск всегда падал
в «Environment not found». Здесь пути собираются через chr(92), а результат
проверяется на управляющие символы; тесты-страховки это тоже стерегут.
"""
from pathlib import Path

BS = chr(92)


def _p(*parts: str) -> str:
    return BS.join(parts)


def build() -> str:
    lines = [
        "@echo off",
        "chcp 866 >nul",
        'cd /d "%~dp0"',
        'set "PMOOS_DATA=%PMOOS_DATA_DIR%"',
        f'if "%PMOOS_DATA%"=="" set "PMOOS_DATA=%USERPROFILE%{_p("", ".pmoos-rag")}"',
        f'if exist "%PMOOS_DATA%{_p("", "venv", "Scripts", "python.exe")}" goto GDATA',
        f'if exist {_p(".venv", "Scripts", "python.exe")} goto GLOCAL',
        "echo [ERROR] Environment not found. Run install.bat first.",
        "pause",
        "exit /b 1",
        ":GDATA",
        "rem -- predupredit, esli posle obnovleniya izmenilis zavisimosti --",
        'set "REQHASH="',
        "for /f \"skip=1 tokens=1\" %%h in ('certutil -hashfile requirements.txt SHA256 2^>nul') do if not defined REQHASH set \"REQHASH=%%h\"",
        f'if not exist "%PMOOS_DATA%{_p("", "venv", "requirements.sha256")}" goto GRUN',
        f'set /p OLDHASH=<"%PMOOS_DATA%{_p("", "venv", "requirements.sha256")}"',
        'if "%REQHASH%"=="%OLDHASH%" goto GRUN',
        "echo ============================================================",
        "echo   VNIMANIE: sostav zavisimostey izmenilsya - nuzhen install.bat",
        "echo   (odin raz posle obnovleniya; potom eto soobshenie ischeznet)",
        "echo ============================================================",
        "pause",
        ":GRUN",
        f'"%PMOOS_DATA%{_p("", "venv", "Scripts", "python.exe")}" {_p("app", "gui", "server.py")}',
        "goto END",
        ":GLOCAL",
        f'{_p(".venv", "Scripts", "python.exe")} {_p("app", "gui", "server.py")}',
        ":END",
        "",
    ]
    text = "\r\n".join(lines)
    bad = [b for b in text.encode("cp866") if b < 32 and b not in (10, 13)]
    if bad:
        raise SystemExit(f"control chars in bat: {bad!r}")
    return text


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    text = build()
    for name in ("СТРОЙРАГ.bat", "run.bat"):
        (root / name).write_bytes(text.encode("cp866"))
        print(f"записан {name} ({len(text)} байт, control-символов нет)")


if __name__ == "__main__":
    main()

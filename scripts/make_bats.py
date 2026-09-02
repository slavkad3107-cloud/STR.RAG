# -*- coding: utf-8 -*-
"""Генерация запускных .bat (CP866, CRLF) БЕЗ backslash-эскейпов в путях.

Урок v0.42.0: «\\venv» в строке, прошедшей через слой эскейпов, превратился в
0x0B (вертикальная табуляция) — .bat уехал в релиз битым, запуск всегда падал
в «Environment not found». Здесь пути собираются через chr(92), а результат
проверяется на управляющие символы; тесты-страховки это тоже стерегут.

v0.44.2: bat запускает сервер в СВЁРНУТОМ окне и САМ открывает браузер после
готовности порта — раньше «чёрное окно висело» (окно сервера оставалось, а
браузер не открывался) и `pause` на предупреждении о зависимостях блокировал
запуск ожиданием клавиши. Теперь без единого pause на нормальном пути.
"""
from pathlib import Path

BS = chr(92)


def _p(*parts: str) -> str:
    return BS.join(parts)


def build() -> str:
    venv_py_con = f'%PMOOS_DATA%{_p("", "venv", "Scripts", "python.exe")}'
    srv = _p("app", "gui", "server.py")
    lines = [
        "@echo off",
        "chcp 866 >nul",
        'cd /d "%~dp0"',
        'set "PMOOS_DATA=%PMOOS_DATA_DIR%"',
        f'if "%PMOOS_DATA%"=="" set "PMOOS_DATA=%USERPROFILE%{_p("", ".pmoos-rag")}"',
        f'set "PYC={venv_py_con}"',
        f'if exist "%PYC%" goto RUN',
        f'set "PYC={_p(".venv", "Scripts", "python.exe")}"',
        f'if exist "%PYC%" goto RUN',
        "echo [ERROR] Environment not found. Run install.bat first.",
        "pause",
        "exit /b 1",
        ":RUN",
        "rem -- napominanie pro install (BEZ pause: ne blokiruet zapusk) --",
        'set "REQHASH="',
        "for /f \"skip=1 tokens=1\" %%h in ('certutil -hashfile requirements.txt SHA256 2^>nul') do if not defined REQHASH set \"REQHASH=%%h\"",
        'set "OLDHASH="',
        f'if exist "%PMOOS_DATA%{_p("", "venv", "requirements.sha256")}" set /p OLDHASH=<"%PMOOS_DATA%{_p("", "venv", "requirements.sha256")}"',
        'if not "%REQHASH%"=="%OLDHASH%" echo [i] Sostav zavisimostey mog izmenitsya - esli chto-to ne rabotaet, zapustite install.bat.',
        "rem -- server v OTDELNOM svyornutom okne; on SAM otkroet brauzer kogda",
        "rem -- budet gotov. Eto okno bat srazu zakryvaetsya.",
        f'start "STROY.RAG" /min "%PYC%" {srv}',
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

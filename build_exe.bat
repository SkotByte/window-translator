@echo off
rem Build dist\WindowTranslator\WindowTranslator.exe (one folder, no console window).
rem models\, data\, tools\ and glossary\ are NOT packed into the exe: they are linked
rem (directory junctions) or copied next to it, so the 4 GB of models are not duplicated.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install -q "pyinstaller>=6.10,<7" || exit /b 1
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --name WindowTranslator ^
  --collect-submodules winrt --collect-binaries ctranslate2 ^
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtPdf --exclude-module PySide6.Qt3DCore --exclude-module tkinter ^
  run_app.py || exit /b 1
for %%d in (models data tools) do (
  if not exist "dist\WindowTranslator\%%d" mklink /J "dist\WindowTranslator\%%d" "%~dp0%%d" >nul
)
xcopy /E /I /Y glossary "dist\WindowTranslator\glossary" >nul
echo.
echo Built: dist\WindowTranslator\WindowTranslator.exe

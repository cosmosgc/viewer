@echo off
setlocal

cd /d "%~dp0"
echo ==========================================
echo Building Cosmos's Galery Manager EXE
echo ==========================================

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  exit /b 1
)

echo [1/3] Ensuring build dependencies are installed...
python -m pip install --upgrade pyinstaller pystray pillow
if errorlevel 1 (
  echo [ERROR] Failed to install/update build dependencies.
  exit /b 1
)

echo [2/3] Cleaning old build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist CosmosGalleryManager.spec del /q CosmosGalleryManager.spec

echo [3/3] Running PyInstaller (onefile)...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --name CosmosGalleryManager ^
  --add-data "templates;templates" ^
  --add-data "pinned_files.json;." ^
  --add-data "resource_inbox;resource_inbox" ^
  resource_viewer.py

if errorlevel 1 (
  echo [ERROR] Build failed.
  exit /b 1
)

copy /y "dist\CosmosGalleryManager.exe" "CosmosGalleryManager.exe" >nul
if errorlevel 1 (
  echo [WARN] Built EXE exists in dist but copy to project root failed.
) else (
  echo [OK] Copied EXE to project root: %cd%\CosmosGalleryManager.exe
)

echo.
echo Build complete.
echo EXE file: %cd%\dist\CosmosGalleryManager.exe
echo Run: dist\CosmosGalleryManager.exe
echo Root copy: %cd%\CosmosGalleryManager.exe
echo.
pause
endlocal

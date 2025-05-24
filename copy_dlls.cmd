@echo off
pushd %~dp0build\%1
set MODULES_DIR=%DEBUGFARHOME%\Plugins\Observer\modules
copy /Y *.so %MODULES_DIR% || exit 1
copy /Y *.pdb %MODULES_DIR%
popd
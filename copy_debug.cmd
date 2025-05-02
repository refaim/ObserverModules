@echo off
pushd %~dp0\buildDir
set MODULES_DIR=%DEBUGFARHOME%\Plugins\Observer\modules
copy /Y *.so %MODULES_DIR% || exit 1
copy /Y *.pdb %MODULES_DIR% || exit 1
popd
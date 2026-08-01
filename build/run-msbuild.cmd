@echo off
setlocal
set "OBSERVER_CLEAN_PATH=%PATH%"
set "OBSERVER_CLEAN_LIB=%LIB%"
set "Path="
set "PATH="
set "Lib="
set "LIB="
set "PATH=%OBSERVER_CLEAN_PATH%"
set "LIB=%OBSERVER_CLEAN_LIB%"
"%OBSERVER_MSBUILD_EXE%" %*
exit /b %ERRORLEVEL%

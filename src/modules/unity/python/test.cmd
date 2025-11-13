@echo off
pushd %~dp0
echo %1
uv run python test.py %1
popd
@echo off
REM Hermes Cluster Server — Windows service wrapper
REM Runs the cluster server and restarts on exit (keep-alive)

set HERMES_CLUSTER_DIR=C:\Users\ahmed\hermes-cluster
set PYTHON=%HERMES_CLUSTER_DIR%\.venv\Scripts\python.exe
set CONFIG=%HERMES_CLUSTER_DIR%\cluster.yaml

:loop
echo [%date% %time%] Starting hermes-cluster server...
%PYTHON% -m hermes_cluster.serve --config %CONFIG% --cluster-id bdaya_hermes_cluster --node-id windows_desktop_main --node-role main
echo [%date% %time%] Server exited with code %ERRORLEVEL%. Restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop

@echo off
echo ========================================================
echo   FlowSync - SUMO Corridor Network Builder
echo ========================================================

netconvert --node-files=corridor.nod.xml --edge-files=corridor.edg.xml --output-file=corridor.net.xml --tls.guess true

if %ERRORLEVEL%==0 (
    echo.
    echo [SUCCESS] Network compiled: corridor.net.xml
    echo You can now run 'python run_comparison.py' or open 'sumo-gui corridor.sumocfg'
) else (
    echo.
    echo [ERROR] Failed to compile network.
    echo Make sure SUMO is installed and SUMO_HOME is added to your environment variables.
    echo Example path: C:\Program Files (x86)\Eclipse\Sumo
)
pause

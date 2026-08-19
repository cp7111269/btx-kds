@echo off
REM ---------------------------------------------------------------
REM  BTX KDS Bridge - start for testing
REM  BT xTech Sdn Bhd
REM
REM  Reads the AutoCount FnB book named in config.json (READ-ONLY) and
REM  serves the kitchen screens to the shop LAN.
REM
REM  The real product ships as a Windows Service via an installer that
REM  also opens the firewall port. This .bat is for testing by hand.
REM ---------------------------------------------------------------
cd /d "%~dp0"
echo.
echo  BTX KDS Bridge
echo  --------------
echo  Settings: config.json
echo  Screens : http://localhost:5180/kds/index.html
echo.
echo  Close this window to stop the Bridge.
echo.
python bridge.py %*
echo.
echo  Bridge stopped.
pause

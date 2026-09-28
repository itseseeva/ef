@echo off
:: kill_python.bat
:: Force-closes all python.exe / pythonw.exe processes, together with
:: any child process they spawned (like the WebView2 host process
:: pywebview uses under the hood). Run this before relaunching
:: EasyFarm if the window ever freezes, or if "Ctrl+C" in the
:: terminal doesn't actually close it.
::
:: Why plain taskkill instead of PowerShell: one file, no execution
:: policy prompts, works out of the box on any Windows PC - simpler
:: is better here since this will also run on customers' machines
:: we don't control, once the app is sold.
::
:: Why this file has NO Russian text in it: a .bat file's encoding
:: is guessed from the customer's Windows code page (866 / 1251 /
:: UTF-8 - all different). Guess wrong and you get exactly the
:: "not recognized as a command" mess we just saw. Plain ASCII text
:: has no encoding to guess, so it behaves identically on every
:: machine regardless of language settings - important once this
:: ships beyond your own PC.
::
:: /F  - force kill (a polite close request can be ignored by a
::       window's own GUI event loop, which is what happened with
::       Ctrl+C and the WebView2 window)
:: /T  - also kill the process tree (child processes), so the
::       WebView2 host process doesn't survive as an orphan
:: /IM - match by image (executable) name instead of PID

echo Closing python.exe and pythonw.exe (and their child processes) ...
taskkill /F /IM python.exe /T
taskkill /F /IM pythonw.exe /T

:: Windows needs a moment to actually release the WebView2 profile
:: lock file after a forced kill (/F). Launching the app again too
:: fast can hit that still-held lock and open a blank/frozen window
:: with no error - this pause makes the wait built-in instead of
:: relying on whoever runs this file to know that and wait manually.
timeout /t 2 >nul

echo Done. Safe to start the app now.
pause

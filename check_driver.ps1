$drivers = Get-CimInstance Win32_SystemDriver
$drivers | Where-Object { $_.Name -match "keyboard|mouse|interception" } | Select-Object Name, State, Status

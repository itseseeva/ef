$events = Get-WinEvent -LogName 'Microsoft-Windows-CodeIntegrity/Operational' -MaxEvents 200 -ErrorAction SilentlyContinue
$blocked = $events | Where-Object { $_.Id -in 3001,3002,3003,3004 -or $_.Message -match "interception" -or $_.Message -match "keyboard" -or $_.Message -match "mouse" }
if ($blocked) {
    $blocked | Select-Object -First 5 | Format-List TimeCreated, Id, Message
} else {
    Write-Host "NO_BLOCKED_EVENTS_FOUND"
}

$ws = New-Object -ComObject WScript.Shell
if ($ws.AppActivate('Universal Game Save Finder')) {
    Start-Sleep -Milliseconds 200
    $ws.SendKeys('%{F4}')
    Start-Sleep -Milliseconds 400
    $ws.SendKeys('y')
} else {
    Write-Output 'App window not found'
}

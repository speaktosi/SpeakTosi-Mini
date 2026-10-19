Get-CimInstance Win32_Process -Filter 'Name="llama-server.exe"' | ForEach-Object {
    Write-Output ("PID=" + $_.ProcessId)
    Write-Output ("CMD=" + $_.CommandLine)
    Write-Output ("CWD=" + (Get-Process -Id $_.ProcessId).Path)
}

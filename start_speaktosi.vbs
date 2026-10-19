' SpeakTosi - silent launcher (no console window, recommended)
' 1019: 启动前静默做一次"路径自愈"(幂等, 路径已正确时 1 秒内返回), 使整包可放在任意目录
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
py = root & "\runtime\py312\python.exe"
setup = root & "\tools\scripts\first_run_setup.py"
If fso.FileExists(py) And fso.FileExists(setup) Then
  sh.CurrentDirectory = root
  sh.Run """" & py & """ """ & setup & """ --quiet", 0, True
End If
sh.CurrentDirectory = root & "\app"
sh.Run "node_modules\electron\dist\electron.exe .", 0, False
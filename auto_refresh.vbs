' Скрытая обёртка для auto_refresh.bat — запускает cmd без окна.
' Task Scheduler вызывает именно этот .vbs вместо .bat.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
shell.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
' Run(cmd, windowStyle=0 hidden, waitOnReturn=False)
shell.Run "cmd /c auto_refresh.bat", 0, False

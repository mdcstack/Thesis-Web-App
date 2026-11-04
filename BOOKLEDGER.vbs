Set WshShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
strScriptDir = objFSO.GetParentFolderName(WScript.ScriptFullName)

' Set working directory to the web app folder
WshShell.CurrentDirectory = strScriptDir

strPythonw = "C:\Users\Matt\AppData\Local\Programs\Python\Python314\pythonw.exe"
If Not objFSO.FileExists(strPythonw) Then
    strPythonw = "pythonw.exe"
End If

strLauncher = strScriptDir & "\launcher.py"
strCommand = """" & strPythonw & """ """ & strLauncher & """"

' 0 = Hide window completely (no black console box)
WshShell.Run strCommand, 0, False


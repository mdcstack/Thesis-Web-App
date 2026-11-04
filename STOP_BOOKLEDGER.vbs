Set WshShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
strScriptDir = objFSO.GetParentFolderName(WScript.ScriptFullName)

WshShell.CurrentDirectory = strScriptDir

' Confirmation prompt (Default = No to prevent accidental shutdowns)
intResponse = MsgBox("Do you want to terminate and stop the BOOKLETLOG background servers?" & vbCrLf & vbCrLf & _
                     "This will close the local HTTPS (8000) and HTTP (8080) servers.", _
                     vbQuestion + vbYesNo + vbDefaultButton2, "BOOKLETLOG - Server Shutdown")

If intResponse = vbYes Then
    strStopBat = strScriptDir & "\stop_server.bat"
    
    ' Run silently in background (0 = hide window, True = wait for completion)
    WshShell.Run """" & strStopBat & """", 0, True
    
    MsgBox "All BOOKLETLOG server processes have been stopped successfully." & vbCrLf & vbCrLf & _
           "Ports 8000 and 8080 are now released.", _
           vbInformation, "BOOKLETLOG - Stopped"
End If


' 视频批量解析 —— 图形界面启动器
' 双击本文件即可打开窗口，全程不会出现命令行黑窗口。
On Error Resume Next

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

PYW = "C:\Users\songyiheng\AppData\Local\Programs\Python\Python312-32\pythonw.exe"
GUI = "C:\Users\songyiheng\.workbuddy\skills\video-batch-insight\gui\video_gui.py"

If Not fso.FileExists(PYW) Then
  MsgBox "找不到运行环境:" & vbCrLf & PYW & vbCrLf & vbCrLf & "请把这句话截图发给我。", 16, "视频批量解析"
  WScript.Quit
End If
If Not fso.FileExists(GUI) Then
  MsgBox "找不到程序文件:" & vbCrLf & GUI & vbCrLf & vbCrLf & "请把这句话截图发给我。", 16, "视频批量解析"
  WScript.Quit
End If

sh.CurrentDirectory = fso.GetParentFolderName(GUI)
sh.Run """" & PYW & """ """ & GUI & """", 0, False

If Err.Number <> 0 Then
  MsgBox "启动失败: " & Err.Description, 16, "视频批量解析"
End If

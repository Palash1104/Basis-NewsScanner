' Starts one newsdesk job with no window at all.
'
' Task Scheduler's own "Hidden" box only hides the *task's* window; a console program still
' gets a console, which is why three black windows flashed up every time the laptop woke
' (user, 2026-09-23). WScript.Shell.Run with a window style of 0 creates the process with its
' window hidden from the start, so nothing is ever drawn.
'
' Usage: wscript.exe //nologo run_hidden.vbs <run|digest|score|serve>
' It waits for the job and passes its exit code back, so Task Scheduler's LastTaskResult
' still means what it always did.

Option Explicit

Dim shell, here, runner, job, command

If WScript.Arguments.Count < 1 Then
  WScript.Quit 2
End If
job = WScript.Arguments(0)

here = Left(WScript.ScriptFullName, Len(WScript.ScriptFullName) - Len(WScript.ScriptName) - 1)
runner = here & "\run_task.ps1"

Set shell = CreateObject("WScript.Shell")
command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & _
          runner & """ -Job " & job

' 0 = hidden window, True = wait for it and return its exit code.
WScript.Quit shell.Run(command, 0, True)

#ifndef PayloadDir
  #define PayloadDir "..\..\dist\ocf-gate-0.0.0"
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppName=OCF Gate
AppVersion={#AppVersion}
AppVerName=OCF Gate {#AppVersion}
AppPublisher=OCF Gate

DefaultDirName={userdocs}
AppendDefaultDirName=no

DirExistsWarning=no
DisableProgramGroupPage=yes
DisableWelcomePage=no
Uninstallable=no
CreateUninstallRegKey=no
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=ocf-gate-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Messages]
SelectDirDesc=Which repository should this gate govern?
SelectDirLabel3=The gate is copied into that repository's own .github folder. A policy.toml or protected.txt already there is kept, and the shipped one is written beside it as .dist.
SelectDirBrowseLabel=Choose the repository folder, then click Next.

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{tmp}\ocf-gate"; Flags: recursesubdirs createallsubdirs ignoreversion

[Code]
var
  PythonExe: String;
  PythonPrefix: String;
  PythonReader: String;

function TryPython(const Exe, Prefix, Module: String): Boolean;
var
  Code: Integer;
begin
  Result := False;
  if not Exec(Exe, Prefix + '-c "import ' + Module + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Exit;
  Result := Code = 0;
end;

procedure DetectPython;
begin
  PythonExe := '';
  if TryPython('python', '', 'tomllib') then begin PythonExe := 'python'; PythonReader := 'tomllib'; Exit; end;
  if TryPython('python', '', 'tomli') then begin PythonExe := 'python'; PythonReader := 'tomli'; Exit; end;
  if TryPython('python3', '', 'tomllib') then begin PythonExe := 'python3'; PythonReader := 'tomllib'; Exit; end;
  if TryPython('python3', '', 'tomli') then begin PythonExe := 'python3'; PythonReader := 'tomli'; Exit; end;
  if TryPython('py', '-3 ', 'tomllib') then begin PythonExe := 'py'; PythonPrefix := '-3 '; PythonReader := 'tomllib'; Exit; end;
  if TryPython('py', '-3 ', 'tomli') then begin PythonExe := 'py'; PythonPrefix := '-3 '; PythonReader := 'tomli'; Exit; end;
end;

function InitializeSetup(): Boolean;
begin
  DetectPython;
  Result := True;
  if PythonExe = '' then begin
    MsgBox('No usable Python was found on PATH.' + #13#10 + #13#10 +
           'The gate needs Python 3.11 or newer, or an older Python with the TOML fallback:' + #13#10 +
           '    pip install "tomli>=2.0.0"' + #13#10 + #13#10 +
           'Nothing was installed. Put the .github folder into the repository by hand instead - it is' + #13#10 +
           'the whole gate - or install Python and run this again.', mbCriticalError, MB_OK);
    Result := False;
  end;
end;

function InstallParameters(Param: String): String;
begin
  Result := PythonPrefix + '"' + ExpandConstant('{tmp}\ocf-gate\.github\ocf\ocf.py') +
            '" install "' + ExpandConstant('{app}') + '"';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  if not Exec(PythonExe, InstallParameters(''), '', SW_SHOW, ewWaitUntilTerminated, Code) then
    Code := -1;
  if Code <> 0 then begin
    MsgBox('The installer failed (exit code ' + IntToStr(Code) + ').' + #13#10 + #13#10 +
           'The console window above holds the reason, and the repository is unchanged by the step' + #13#10 +
           'that failed. If it keeps failing, put the .github folder into the repository by hand -' + #13#10 +
           'it is the whole gate.', mbCriticalError, MB_OK);
    Exit;
  end;
  MsgBox('The gate is installed into:' + #13#10 + ExpandConstant('{app}') + #13#10 + #13#10 +
         'What is left is yours, and the console above listed it in full:' + #13#10 +
         '  1. reload the VS Code window - hooks are read when the window starts' + #13#10 +
         '  2. record what the machine may not change, arm the gate, and run reload' + #13#10 + #13#10 +
         'One thing the installer cannot check for you: the chat permission level must not be' + #13#10 +
         'Autopilot, and chat.autoReply must be false. With either one on, VS Code answers an' + #13#10 +
         'agent question by itself, and the gate cannot tell that answer from a human one.' + #13#10 + #13#10 +
         'The details are in section 2 of .github\work-control-flow.md in that repository.',
         mbInformation, MB_OK);
end;

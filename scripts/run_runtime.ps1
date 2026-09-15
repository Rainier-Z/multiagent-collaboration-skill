[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$Script,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-StructuredError {
    param(
        [string]$Code,
        [string]$Message,
        [object[]]$Candidates
    )

    [ordered]@{
        status = 'ERROR'
        error_code = $Code
        message = $Message
        candidates = @($Candidates)
    } | ConvertTo-Json -Compress -Depth 6
}

function Test-PythonCandidate {
    param(
        [string]$Path,
        [string[]]$Prefix = @(),
        [bool]$NeedDocx = $false,
        [string]$Source
    )

    $record = [ordered]@{ source = $Source; path = $Path; prefix = @($Prefix); usable = $false }
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $record.reason = 'not_found'
        return [pscustomobject]$record
    }

    try {
        $version = & $Path @Prefix -X utf8 -c 'import sys; print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1]))' 2>&1
        if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+$') {
            $record.reason = 'not_a_working_python'
            $record.detail = ($version | Out-String).Trim()
            return [pscustomobject]$record
        }
        $parts = $version.Split('.') | ForEach-Object { [int]$_ }
        if ($parts[0] -lt 3 -or ($parts[0] -eq 3 -and $parts[1] -lt 11)) {
            $record.reason = 'python_lt_3_11'
            $record.version = $version
            return [pscustomobject]$record
        }
        if ($NeedDocx) {
            $probe = & $Path @Prefix -X utf8 -c 'import docx; print(docx.__version__)' 2>&1
            if ($LASTEXITCODE -ne 0) {
                $record.reason = 'missing_python_docx'
                $record.version = $version
                return [pscustomobject]$record
            }
            $record.docx_version = ($probe | Out-String).Trim()
        }
        $record.usable = $true
        $record.version = $version
        return [pscustomobject]$record
    }
    catch {
        $record.reason = 'probe_failed'
        $record.detail = $_.Exception.Message
        return [pscustomobject]$record
    }
}

function Add-Candidate {
    param([System.Collections.Generic.List[object]]$List, [string]$Path, [string[]]$Prefix, [string]$Source)
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    $key = "$Path|$($Prefix -join ' ')"
    if ($script:CandidateKeys.Add($key)) {
        $List.Add([pscustomobject]@{ Path = $Path; Prefix = @($Prefix); Source = $Source })
    }
}

$scriptRoot = Split-Path -Parent $PSCommandPath
$skillRoot = Split-Path -Parent $scriptRoot
$scriptsRoot = [IO.Path]::GetFullPath($scriptRoot)
$requestedPath = [IO.Path]::GetFullPath((Join-Path $scriptsRoot $Script))
$insideScripts = $requestedPath.StartsWith(($scriptsRoot + [IO.Path]::DirectorySeparatorChar), [System.StringComparison]::OrdinalIgnoreCase)
$targetExists = Test-Path -LiteralPath $requestedPath -PathType Leaf
if ($insideScripts -eq $false -or $targetExists -eq $false) {
    $invalidCandidate = [pscustomobject]@{ requested = $Script }
    Write-StructuredError "E_SCRIPT_PATH" "script_path_invalid" @($invalidCandidate)
    exit 2
}

$needDocx = ([IO.Path]::GetFileName($requestedPath) -ieq "export_docx.py")
$candidates = [System.Collections.Generic.List[object]]::new()
$script:CandidateKeys = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)

if (-not [string]::IsNullOrWhiteSpace($env:MULTIAGENT_PYTHON)) {
    Add-Candidate $candidates $env:MULTIAGENT_PYTHON @() "MULTIAGENT_PYTHON"
}

$pyCommand = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $pyCommand -and $pyCommand.CommandType -eq 'Application') {
    Add-Candidate $candidates $pyCommand.Source @("-3") "py -3"
}
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $pythonCommand -and $pythonCommand.CommandType -eq 'Application') {
    Add-Candidate $candidates $pythonCommand.Source @() "python"
}

if (-not [string]::IsNullOrWhiteSpace($env:ProgramData)) {
    Add-Candidate $candidates (Join-Path $env:ProgramData "anaconda3\python.exe") @() "ProgramData Anaconda"
}

if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    Add-Candidate $candidates (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe") @() "USERPROFILE Codex bundled Python"
    $codexRoot = Join-Path $env:USERPROFILE ".codex"
    if (Test-Path -LiteralPath $codexRoot -PathType Container) {
        Get-ChildItem -LiteralPath $codexRoot -Filter python.exe -File -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName |
            ForEach-Object { Add-Candidate $candidates $_.FullName @() "USERPROFILE Codex bundled Python" }
    }
}

$results = [System.Collections.Generic.List[object]]::new()
foreach ($candidate in $candidates) {
    $probe = Test-PythonCandidate -Path $candidate.Path -Prefix $candidate.Prefix -NeedDocx $needDocx -Source $candidate.Source
    $results.Add($probe)
    if ($probe.usable) {
        Write-Output (([ordered]@{ status = 'READY'; interpreter = $probe.path; source = $probe.source; version = $probe.version; requires_docx = $needDocx } | ConvertTo-Json -Compress))
        & $candidate.Path @($candidate.Prefix) -X utf8 $requestedPath @ScriptArgs
        exit $LASTEXITCODE
    }
}

$code = if ($needDocx -and ($results | Where-Object { $_.reason -eq "missing_python_docx" })) { "E_PYTHON_DOCX_UNAVAILABLE" } else { "E_PYTHON_UNAVAILABLE" }
Write-StructuredError $code "No Python 3.11+ interpreter satisfies this script dependency; target was not executed." $results.ToArray()
exit 1

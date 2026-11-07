param(
    [string]$Python = '.venv\Scripts\python.exe',
    [string]$EvidenceDir = 'docs\generated\public-ci'
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = if ([IO.Path]::IsPathRooted($Python)) { $Python } else { Join-Path $root $Python }
$evidence = if ([IO.Path]::IsPathRooted($EvidenceDir)) { $EvidenceDir } else { Join-Path $root $EvidenceDir }
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw "Python not found: $pythonPath" }
if (Test-Path -LiteralPath $evidence) { throw "Evidence directory exists; use a new run directory: $evidence" }
New-Item -ItemType Directory -Path $evidence | Out-Null
Push-Location $root
try {
    # These two unchanged tests require private historical byte manifests, not runtime inputs.
    # Public CI is NOT the internal acceptance pipeline. Do not silently skip other tests.
    $pytest = @(
        '-m', 'pytest', '-q', '-o', 'junit_family=legacy',
        '--deselect=tests/phase3/test_guards.py::test_phase2_core_original_tests_and_dataset_remain_unchanged',
        '--deselect=tests/phase3/test_guards.py::test_historical_archives_are_byte_identical',
        ('--junitxml=' + (Join-Path $evidence 'pytest.xml'))
    )
    $checks = @(
        @{ Name = 'pip-check'; Arguments = @('-m', 'pip', 'check') },
        @{ Name = 'ruff-check'; Arguments = @('-m', 'ruff', 'check', 'app', 'tests', 'scripts') },
        @{ Name = 'ruff-format'; Arguments = @('-m', 'ruff', 'format', '--check', 'app', 'tests', 'scripts') },
        @{ Name = 'compile'; Arguments = @('-m', 'compileall', '-q', 'app', 'tests', 'scripts') },
        @{ Name = 'pytest-public'; Arguments = $pytest },
        @{ Name = 'openapi-runtime'; Arguments = @((Join-Path $root 'scripts\verify_public_openapi.py'), (Join-Path $evidence 'pytest.xml'), (Join-Path $evidence 'openapi-runtime.json')) }
    )
    $results = @()
    foreach ($check in $checks) {
        $arguments = $check.Arguments
        $ErrorActionPreference = 'Continue'
        & $pythonPath @arguments 1> (Join-Path $evidence ($check.Name + '.stdout.log')) 2> (Join-Path $evidence ($check.Name + '.stderr.log'))
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        $results += [pscustomobject]@{ name = $check.Name; exit_code = $code }
        Write-Output "$($check.Name): exit $code"
        if ($code -ne 0) {
            Get-Content -LiteralPath (Join-Path $evidence ($check.Name + '.stdout.log')) -Tail 60
            Get-Content -LiteralPath (Join-Path $evidence ($check.Name + '.stderr.log')) -Tail 60
            break
        }
        if ($check.Name -eq 'pytest-public') {
            Get-Content -LiteralPath (Join-Path $evidence ($check.Name + '.stdout.log')) -Tail 2
        }
        if ($check.Name -eq 'openapi-runtime') {
            Get-Content -LiteralPath (Join-Path $evidence ($check.Name + '.stdout.log'))
        }
    }
    $results | ConvertTo-Json -Depth 4 | Out-File -LiteralPath (Join-Path $evidence 'results.json') -Encoding utf8
    if ($results.Count -ne $checks.Count -or @($results | Where-Object exit_code -ne 0).Count -ne 0) { exit 1 }
} finally { Pop-Location }
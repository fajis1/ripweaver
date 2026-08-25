[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9._-]*$')]
    [string]$Channel,

    [string]$Remote = 'origin',

    [switch]$Push,

    [switch]$Preview
)

$ErrorActionPreference = 'Stop'

$repoRoot = (& git rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0 -or -not $repoRoot) {
    throw 'Run this script from inside the RipWeaver Git repository.'
}

if ($Remote -ne 'origin') {
    throw 'Checkpoint pushes may target only the owner-controlled origin remote.'
}

$sourceBranch = (& git -C $repoRoot branch --show-current).Trim()
$sourceHead = (& git -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $sourceHead) {
    throw 'The current Git HEAD could not be resolved.'
}

if ($Channel -eq 'main' -and $sourceBranch -ne 'main') {
    throw "The main checkpoint may only be created while the main branch is checked out (current: $sourceBranch)."
}
if ($Channel -eq 'test' -and $sourceBranch -eq 'main') {
    throw 'Use -Channel main while the main branch is checked out.'
}

$checkpointRef = "refs/heads/wip/$Channel"
$checkpointIndexPath = [IO.Path]::Combine(
    [IO.Path]::GetTempPath(),
    "ripweaver-checkpoint-$([Guid]::NewGuid().ToString('N')).index"
)
$resolvedTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$resolvedCheckpointIndex = [IO.Path]::GetFullPath($checkpointIndexPath)
if (-not $resolvedCheckpointIndex.StartsWith($resolvedTempRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The temporary checkpoint index did not resolve inside the Windows temporary directory.'
}

$hadPreviousGitIndex = Test-Path Env:GIT_INDEX_FILE
$previousGitIndex = $env:GIT_INDEX_FILE
$env:GIT_INDEX_FILE = $resolvedCheckpointIndex

try {
    & git -C $repoRoot read-tree HEAD
    if ($LASTEXITCODE -ne 0) { throw 'The isolated checkpoint index could not be initialized.' }

    # Snapshot the meaningful repository state without touching the user's real
    # index or worktree. These exclusions are intentionally conservative for a
    # public repository: no local environments, media, recovery transcripts,
    # pytest scratch directories, coverage data, logs, or ad-hoc repair scripts.
    $checkpointExclusions = @(
        '.env',
        '.env.*',
        '**/.env',
        '**/.env.*',
        '.coverage',
        '.pytest-*',
        '**/.pytest-*',
        '.pytest*/**',
        '**/.pytest*/**',
        '.claude/**',
        '.agents/**',
        '.codex/**',
        'patch.py',
        'patch.js',
        'check_drives.py',
        'check_jobs.py',
        '**/*.log',
        '.mkv-preflight/**',
        '**/*.mkv',
        '**/*.iso',
        '**/*.mp4',
        '**/*.wav'
    )
    # Exclude private/scratch trees during traversal as well as resetting them
    # afterward. Stage tracked changes separately, then enumerate accessible
    # untracked files. A locked pytest scratch directory must not prevent
    # unrelated reviewed work from receiving an emergency checkpoint.
    $checkpointAddPathspecs = @('.')
    $checkpointAddPathspecs += @(
        $checkpointExclusions | ForEach-Object { ":(exclude)$_" }
    )
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $null = & git -C $repoRoot -c core.safecrlf=false add -u -- @checkpointAddPathspecs 2>&1
    $trackedAddExitCode = $LASTEXITCODE
    $untrackedFiles = @(
        & git -C $repoRoot ls-files --others --exclude-standard -- @checkpointAddPathspecs 2>$null
    )
    $untrackedListExitCode = $LASTEXITCODE
    $untrackedAddExitCode = 0
    if ($untrackedFiles.Count -gt 0) {
        $null = & git -C $repoRoot -c core.safecrlf=false add -- @untrackedFiles 2>&1
        $untrackedAddExitCode = $LASTEXITCODE
    }
    $null = & git -C $repoRoot reset -q HEAD -- @checkpointExclusions 2>&1
    $resetExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorActionPreference
    if ($trackedAddExitCode -ne 0 -or $untrackedListExitCode -ne 0 -or $untrackedAddExitCode -ne 0) {
        throw (
            'The isolated checkpoint index could not stage the reviewed repository paths ' +
            "(tracked=$trackedAddExitCode, list=$untrackedListExitCode, untracked=$untrackedAddExitCode, " +
            "paths=$($checkpointAddPathspecs.Count), files=$($untrackedFiles.Count))."
        )
    }
    if ($resetExitCode -ne 0) { throw 'The excluded local paths could not be removed from the isolated checkpoint index.' }

    $candidateFiles = @(& git -C $repoRoot diff --cached --name-only --diff-filter=ACDMRTUXB)
    if ($LASTEXITCODE -ne 0) { throw 'The checkpoint file list could not be inspected.' }

    $forbiddenFiles = @($candidateFiles | Where-Object {
        $_ -match '(^|/)\.env($|\.)' -or
        $_ -match '\.(pem|p12|pfx|key)$' -or
        $_ -match '(^|/)(credentials|secrets?)(/|$)'
    })
    if ($forbiddenFiles.Count -gt 0) {
        throw "Checkpoint refused because $($forbiddenFiles.Count) secret-like path(s) were selected."
    }

    # Check a small set of high-confidence credential signatures without ever
    # printing the matching line or value. GitHub may reject a secret later,
    # but a public recovery branch should stop before attempting that push.
    $credentialPattern = '-----BEGIN [A-Z ]*PRIVATE KEY-----|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|AKIA[0-9A-Z]{16}'
    $credentialMatches = @(& git -C $repoRoot grep --cached -I -E -e $credentialPattern -- . 2>$null)
    if ($LASTEXITCODE -notin @(0, 1)) { throw 'The staged credential scan could not be completed.' }
    if ($credentialMatches.Count -gt 0) {
        throw "Checkpoint refused because $($credentialMatches.Count) possible credential occurrence(s) were detected."
    }

    $userProfilePath = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    if ($userProfilePath) {
        $profileForms = @($userProfilePath, ($userProfilePath -replace '\\', '/')) | Select-Object -Unique
        foreach ($profileForm in $profileForms) {
            $personalPathMatches = @(& git -C $repoRoot grep --cached -I -i -F -e $profileForm -- . 2>$null)
            if ($LASTEXITCODE -notin @(0, 1)) { throw 'The staged personal-path scan could not be completed.' }
            if ($personalPathMatches.Count -gt 0) {
                throw "Checkpoint refused because $($personalPathMatches.Count) local user-profile path occurrence(s) were detected."
            }
        }
    }

    $maximumBlobBytes = 25MB
    foreach ($candidateFile in $candidateFiles) {
        $indexLine = (& git -C $repoRoot ls-files -s -- $candidateFile | Select-Object -First 1)
        if (-not $indexLine -or $indexLine -notmatch '^\d+\s+([0-9a-f]+)\s+\d+\s+') { continue }
        $blobBytes = [int64]((& git -C $repoRoot cat-file -s $Matches[1]).Trim())
        if ($blobBytes -gt $maximumBlobBytes) {
            throw "Checkpoint refused because a selected file exceeds the 25 MiB safety limit: $candidateFile"
        }
    }

    Write-Host "Checkpoint channel: wip/$Channel"
    Write-Host "Source branch: $sourceBranch"
    Write-Host "Reviewed changed paths: $($candidateFiles.Count)"

    $checkpointTree = (& git -C $repoRoot write-tree).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $checkpointTree) { throw 'The checkpoint tree could not be written.' }

    if ($Preview) {
        Write-Host 'Preview complete. The current branch, real index, and worktree were not changed.'
        exit 0
    }

    $null = & git -C $repoRoot show-ref --verify --quiet $checkpointRef
    $checkpointLookupExitCode = $LASTEXITCODE
    if ($checkpointLookupExitCode -eq 0) {
        $existingCheckpoint = (& git -C $repoRoot rev-parse $checkpointRef).Trim()
    } elseif ($checkpointLookupExitCode -eq 1) {
        $existingCheckpoint = $null
    } else {
        throw 'The existing checkpoint reference could not be inspected.'
    }

    $existingTree = $null
    if ($existingCheckpoint) {
        $existingTree = (& git -C $repoRoot rev-parse "$existingCheckpoint`^{tree}").Trim()
        if ($LASTEXITCODE -ne 0 -or -not $existingTree) { throw 'The existing checkpoint tree could not be resolved.' }
    }
    $sourceTree = (& git -C $repoRoot rev-parse "$sourceHead`^{tree}").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $sourceTree) { throw 'The source tree could not be resolved.' }

    if ($existingCheckpoint -and $existingTree -eq $checkpointTree) {
        Write-Host 'The checkpoint already contains this exact worktree snapshot.'
    } elseif (-not $existingCheckpoint -and $checkpointTree -eq $sourceTree) {
        & git -C $repoRoot update-ref $checkpointRef $sourceHead
        if ($LASTEXITCODE -ne 0) { throw 'The initial local checkpoint reference could not be created.' }
    } else {
        $parentCommit = if ($existingCheckpoint) { $existingCheckpoint } else { $sourceHead }
        $timestamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        $commitMessage = "WIP checkpoint [$Channel]: $timestamp`n`nSource branch: $sourceBranch`nSource HEAD: $sourceHead"
        $checkpointCommit = ($commitMessage | & git -C $repoRoot commit-tree $checkpointTree -p $parentCommit).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $checkpointCommit) { throw 'The checkpoint commit could not be created.' }

        if ($existingCheckpoint) {
            & git -C $repoRoot update-ref $checkpointRef $checkpointCommit $existingCheckpoint
        } else {
            & git -C $repoRoot update-ref $checkpointRef $checkpointCommit
        }
        if ($LASTEXITCODE -ne 0) { throw 'The local checkpoint reference could not be updated.' }
    }

    $resolvedCheckpoint = (& git -C $repoRoot rev-parse $checkpointRef).Trim()
    Write-Host "Local checkpoint: $($resolvedCheckpoint.Substring(0, 12))"

    if ($Push) {
        & git -C $repoRoot push $Remote "$checkpointRef`:$checkpointRef"
        if ($LASTEXITCODE -ne 0) { throw "The checkpoint could not be pushed to $Remote." }
        Write-Host "Remote checkpoint updated: $Remote/wip/$Channel"
    } else {
        Write-Host 'Remote push skipped. Re-run with -Push to create the off-machine backup.'
    }
} finally {
    if ($hadPreviousGitIndex) {
        $env:GIT_INDEX_FILE = $previousGitIndex
    } else {
        Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
    }
    if ([IO.File]::Exists($resolvedCheckpointIndex)) {
        [IO.File]::Delete($resolvedCheckpointIndex)
    }
}

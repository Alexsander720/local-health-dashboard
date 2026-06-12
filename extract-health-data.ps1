param(
    [string]$AdbPath = "C:\scrcpy-win64-v3.3.4\adb.exe",
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"

$toolboxPackage = "androidx.health.connect.client.devtool"
$toolboxApk = Join-Path $PSScriptRoot "HealthConnectToolbox-2_3_3.apk"

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $PSScriptRoot "runs"
}

$runDir = Join-Path $OutputRoot (Get-Date -Format "yyyyMMdd-HHmmss")
$null = New-Item -ItemType Directory -Path $runDir -Force
$null = New-Item -ItemType Directory -Path (Join-Path $runDir "raw") -Force
$null = New-Item -ItemType Directory -Path (Join-Path $runDir "toolbox") -Force
$null = New-Item -ItemType Directory -Path (Join-Path $runDir "pulls") -Force

function Write-Section {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message =="
}

function Invoke-Adb {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [object[]]$Args
    )

    $flatArgs = foreach ($arg in $Args) {
        if ($arg -is [System.Array]) {
            foreach ($nested in $arg) {
                [string]$nested
            }
        } else {
            [string]$arg
        }
    }

    & $AdbPath @flatArgs
}

function Save-Text {
    param(
        [string]$Path,
        [string[]]$Lines
    )

    $parent = Split-Path -Parent $Path
    if ($parent) {
        $null = New-Item -ItemType Directory -Path $parent -Force
    }
    if ($null -eq $Lines) {
        $Lines = @()
    }
    [System.IO.File]::WriteAllLines($Path, $Lines)
}

function Get-DeviceState {
    $lines = Invoke-Adb @("devices", "-l")
    $deviceLines = $lines | Where-Object { $_ -match "^\S+\s+device\b" }
    if (-not $deviceLines) {
        throw "ADB device not found. Unlock the phone and confirm USB debugging."
    }
    return $deviceLines[0]
}

function Get-ScreenSize {
    $line = (Invoke-Adb shell wm size | Select-String "Physical size").ToString()
    if ($line -notmatch "(\d+)x(\d+)") {
        throw "Could not read screen size."
    }

    [pscustomobject]@{
        Width = [int]$Matches[1]
        Height = [int]$Matches[2]
    }
}

function Invoke-Tap {
    param(
        [double]$XRatio,
        [double]$YRatio,
        [int]$DelayMs = 600
    )

    $x = [math]::Round($script:Screen.Width * $XRatio)
    $y = [math]::Round($script:Screen.Height * $YRatio)
    Invoke-Adb shell input tap $x $y | Out-Null
    Start-Sleep -Milliseconds $DelayMs
}

function Invoke-Keyevent {
    param(
        [int]$Code,
        [int]$Count = 1,
        [int]$DelayMs = 40
    )

    foreach ($index in 1..$Count) {
        Invoke-Adb shell input keyevent $Code | Out-Null
        Start-Sleep -Milliseconds $DelayMs
    }
}

function Invoke-TextInput {
    param([string]$Text)

    $escaped = $Text -replace " ", "%s"
    Invoke-Adb shell input text $escaped | Out-Null
    Start-Sleep -Milliseconds 500
}

function Dump-Ui {
    param(
        [string]$RemoteFileName,
        [string]$LocalPath
    )

    Invoke-Adb shell uiautomator dump "/sdcard/$RemoteFileName" | Out-Null
    Invoke-Adb pull "/sdcard/$RemoteFileName" $LocalPath | Out-Null
}

function Get-XmlTexts {
    param([string]$XmlPath)

    [xml]$xml = Get-Content -LiteralPath $XmlPath
    $texts = New-Object System.Collections.Generic.List[string]

    function Visit-Node {
        param($Node)

        if ($null -eq $Node) {
            return
        }

        if ($Node.Attributes) {
            $textAttr = $Node.Attributes["text"]
            if ($textAttr -and -not [string]::IsNullOrWhiteSpace($textAttr.Value)) {
                $texts.Add($textAttr.Value)
            }
        }

        foreach ($child in $Node.ChildNodes) {
            Visit-Node -Node $child
        }
    }

    Visit-Node -Node $xml.DocumentElement
    return $texts
}

function Ensure-ToolboxInstalled {
    $installed = Invoke-Adb shell pm list packages $toolboxPackage
    if ($installed -match [regex]::Escape($toolboxPackage)) {
        return
    }

    if (-not (Test-Path -LiteralPath $toolboxApk)) {
        throw "Toolbox APK not found at $toolboxApk"
    }

    Write-Section "Installing Health Connect Toolbox"
    Invoke-Adb @("install", "-r", $toolboxApk) | Out-Null
}

function Grant-ToolboxHealthPermissions {
    Write-Section "Granting Toolbox Permissions"

    $permissions =
        Invoke-Adb shell dumpsys package $toolboxPackage |
        Select-String "^\s*android\.permission\.health\." |
        ForEach-Object { $_.ToString().Trim() } |
        Where-Object { $_ -notmatch ": granted=" } |
        Sort-Object -Unique

    foreach ($permission in $permissions) {
        try {
            Invoke-Adb shell pm grant $toolboxPackage $permission 2>$null | Out-Null
        } catch {
            Write-Warning "Could not grant $permission"
        }
    }

    Save-Text -Path (Join-Path $runDir "raw\toolbox-health-permissions.txt") -Lines $permissions
}

function Open-ToolboxReadScreen {
    Write-Section "Opening Toolbox Read Screen"
    Invoke-Adb @("shell", "am", "force-stop", $toolboxPackage) | Out-Null
    Invoke-Adb @("shell", "monkey", "-p", $toolboxPackage, "-c", "android.intent.category.LAUNCHER", "1") | Out-Null
    Start-Sleep -Seconds 2

    # Home screen: "Read Health Record"
    Invoke-Tap -XRatio 0.5 -YRatio 0.58125 -DelayMs 1200
}

function Capture-HealthConnectScreens {
    Write-Section "Capturing Health Connect Screens"
    Invoke-Adb @(
        "shell",
        "am",
        "start",
        "-a",
        "android.health.connect.action.HEALTH_HOME_SETTINGS",
        "-n",
        "com.android.healthconnect.controller/.navigation.TrampolineActivity"
    ) | Out-Null
    Start-Sleep -Seconds 2

    $homePath = Join-Path $runDir "raw\health-connect-home.xml"
    Dump-Ui -RemoteFileName "health-connect-home.xml" -LocalPath $homePath

    # Main screen item: "Разрешения для приложений"
    Invoke-Tap -XRatio 0.5 -YRatio 0.546875 -DelayMs 1200

    $appsPath = Join-Path $runDir "raw\health-connect-apps.xml"
    Dump-Ui -RemoteFileName "health-connect-apps.xml" -LocalPath $appsPath
}

function Pull-IfExists {
    param(
        [string]$RemotePath,
        [string]$LocalPath
    )

    try {
        Invoke-Adb pull $RemotePath $LocalPath | Out-Null
    } catch {
        Write-Warning "Could not pull $RemotePath"
    }
}

function Query-ToolboxType {
    param([string]$TypeName)

    Write-Host "Querying $TypeName"

    Invoke-Tap -XRatio 0.5 -YRatio 0.1825 -DelayMs 300
    Invoke-Keyevent -Code 67 -Count 30 -DelayMs 30
    Invoke-TextInput -Text $TypeName

    Invoke-Adb @("logcat", "-c") | Out-Null

    # Query screen: "READ"
    Invoke-Tap -XRatio 0.745 -YRatio 0.489 -DelayMs 2200

    $safeName = ($TypeName -replace "[^A-Za-z0-9_-]", "_")
    $xmlPath = Join-Path $runDir "toolbox\$safeName.xml"
    Dump-Ui -RemoteFileName "toolbox-$safeName.xml" -LocalPath $xmlPath

    $logLines =
        Invoke-Adb @("logcat", "-d") |
        Select-String "ToolboxToastMessage|HealthConnectManager|aggregateData" |
        ForEach-Object { $_.ToString() }

    Save-Text -Path (Join-Path $runDir "toolbox\$safeName.log.txt") -Lines $logLines

    $allTexts = Get-XmlTexts -XmlPath $xmlPath
    Save-Text -Path (Join-Path $runDir "toolbox\$safeName.text.txt") -Lines $allTexts

    $noise = @(
        "Select Data Type",
        "Data Type",
        "Start Time",
        "End Time",
        "Date",
        "Time",
        "RESET",
        "READ",
        "Read Health Record",
        $TypeName
    )

    $interesting =
        $allTexts |
        Where-Object { $_ -and ($_ -notin $noise) } |
        Select-Object -Unique

    [pscustomobject]@{
        Type = $TypeName
        InterestingText = if ($interesting) { $interesting -join " | " } else { "" }
        LogLines = if ($logLines) { $logLines -join " || " } else { "" }
        HasData = [bool]($interesting -or $logLines)
    }
}

Write-Section "Checking Environment"

if (-not (Test-Path -LiteralPath $AdbPath)) {
    throw "ADB not found at $AdbPath"
}

$deviceLine = Get-DeviceState
$script:Screen = Get-ScreenSize

Save-Text -Path (Join-Path $runDir "raw\device.txt") -Lines @(
    "Device: $deviceLine",
    "Screen: $($script:Screen.Width)x$($script:Screen.Height)",
    ("Model: " + (Invoke-Adb shell getprop ro.product.model)),
    ("Android: " + (Invoke-Adb shell getprop ro.build.version.release)),
    ("SDK: " + (Invoke-Adb shell getprop ro.build.version.sdk))
)

Save-Text -Path (Join-Path $runDir "raw\packages.txt") -Lines (
    Invoke-Adb shell pm list packages |
    Select-String "mobvoi|fit|google|zepp|huami|health|yazio|xiaomi" |
    ForEach-Object { $_.ToString() }
)

Save-Text -Path (Join-Path $runDir "raw\mobvoi-health-permissions.txt") -Lines (
    Invoke-Adb shell dumpsys package com.mobvoi.companion.at |
    Select-String "android.permission.health." |
    ForEach-Object { $_.ToString() }
)

Save-Text -Path (Join-Path $runDir "raw\yazio-health-permissions.txt") -Lines (
    Invoke-Adb shell dumpsys package com.yazio.android |
    Select-String "android.permission.health." |
    ForEach-Object { $_.ToString() }
)

Save-Text -Path (Join-Path $runDir "raw\fit-health-permissions.txt") -Lines (
    Invoke-Adb shell dumpsys package com.google.android.apps.fitness |
    Select-String "android.permission.health." |
    ForEach-Object { $_.ToString() }
)

Save-Text -Path (Join-Path $runDir "raw\mobvoi-files.txt") -Lines (
    Invoke-Adb shell "find /sdcard/Android/data/com.mobvoi.companion.at -maxdepth 5 -type f 2>/dev/null"
)

Save-Text -Path (Join-Path $runDir "raw\healthdata-files.txt") -Lines (
    Invoke-Adb shell "find /sdcard/Android/data/com.google.android.apps.healthdata -maxdepth 5 -type f 2>/dev/null"
)

Save-Text -Path (Join-Path $runDir "raw\zepp-files.txt") -Lines (
    Invoke-Adb shell "find /sdcard/Android/data/com.xiaomi.hm.health -maxdepth 5 -type f 2>/dev/null"
)

Save-Text -Path (Join-Path $runDir "raw\yazio-files.txt") -Lines (
    Invoke-Adb shell "find /sdcard/Android/data/com.yazio.android -maxdepth 5 -type f 2>/dev/null"
)

Pull-IfExists -RemotePath "/sdcard/Android/data/com.mobvoi.companion.at" -LocalPath (Join-Path $runDir "pulls\com.mobvoi.companion.at")
Pull-IfExists -RemotePath "/sdcard/Android/data/com.xiaomi.hm.health" -LocalPath (Join-Path $runDir "pulls\com.xiaomi.hm.health")
Pull-IfExists -RemotePath "/sdcard/Android/data/com.yazio.android" -LocalPath (Join-Path $runDir "pulls\com.yazio.android")
Pull-IfExists -RemotePath "/sdcard/Android/data/com.google.android.apps.healthdata" -LocalPath (Join-Path $runDir "pulls\com.google.android.apps.healthdata")

Capture-HealthConnectScreens
Ensure-ToolboxInstalled
Grant-ToolboxHealthPermissions
Open-ToolboxReadScreen

$typesToQuery = @(
    "Weight",
    "Nutrition",
    "Steps",
    "SleepSession",
    "HeartRate",
    "RestingHeartRate",
    "OxygenSaturation",
    "Vo2Max",
    "ExerciseSession",
    "Distance",
    "ActiveCaloriesBurned",
    "TotalCaloriesBurned",
    "FloorsClimbed",
    "Hydration",
    "RespiratoryRate",
    "BasalMetabolicRate",
    "BodyFat",
    "Height"
)

$results = foreach ($typeName in $typesToQuery) {
    Query-ToolboxType -TypeName $typeName
}

$results |
    Sort-Object Type |
    ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath (Join-Path $runDir "toolbox\summary.json") -Encoding UTF8

$results |
    Sort-Object Type |
    Format-Table Type, HasData, InterestingText -AutoSize |
    Out-String |
    Set-Content -LiteralPath (Join-Path $runDir "toolbox\summary.txt") -Encoding UTF8

Write-Section "Done"
Write-Host "Saved run to $runDir"

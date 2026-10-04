# ================================================================
#  Water Inspection System - Windows Setup Script
#  Run in PowerShell as Administrator:
#    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#    .\setup-windows.ps1
# ================================================================

param(
    [switch]$SkipUSB,
    [switch]$BuildLocal,
    [string]$CameraUSBID = "",
    [string]$ScannerUSBID = ""
)

$ErrorActionPreference = "Stop"
$ComposeFile = "docker-compose.windows.yml"

function Write-Step { param($n, $msg) Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Write-OK   { param($msg)      Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Warn { param($msg)      Write-Host "    WARN: $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg)      Write-Host "    FAIL: $msg" -ForegroundColor Red }

# ── 1. Docker Desktop check ──────────────────────────────────────
Write-Step 1 "Checking Docker Desktop..."
try {
    $v = docker version --format "{{.Server.Version}}" 2>$null
    Write-OK "Docker $v is running"
} catch {
    Write-Fail "Docker Desktop is not running. Start it first."
    exit 1
}

# ── 2. Data folder structure ─────────────────────────────────────
Write-Step 2 "Creating data/ folder structure..."
$folders = @("data", "data\results", "data\captures", "data\enhanced_images", "data\logs")
foreach ($f in $folders) {
    if (-not (Test-Path $f)) {
        New-Item -ItemType Directory -Path $f | Out-Null
        Write-OK "Created $f"
    } else {
        Write-OK "$f already exists"
    }
}

# ── 3. .env file ─────────────────────────────────────────────────
Write-Step 3 "Checking .env file..."
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Warn ".env created from .env.example -- EDIT IT and add your API keys!"
        Write-Warn "  GROQ_API_KEY=gsk_..."
        Write-Warn "  GEMINI_API_KEY=AIza..."
        notepad ".env"
    } else {
        Write-Fail ".env.example not found. Create .env manually."
        exit 1
    }
} else {
    Write-OK ".env exists"
}

# ── 4. Required data files check ─────────────────────────────────
Write-Step 4 "Checking required data files..."
$required = @("data\program_mapping.xlsx", "data\web_point.db")
$missing = @()
foreach ($f in $required) {
    if (Test-Path $f) { Write-OK "$f found" }
    else {
        Write-Warn "$f is MISSING -- copy it to data\ before running"
        $missing += $f
    }
}

# ── 5. usbipd-win for camera + scanner ───────────────────────────
if (-not $SkipUSB) {
    Write-Step 5 "Checking usbipd-win for USB device passthrough..."
    $usbipd = Get-Command usbipd -ErrorAction SilentlyContinue
    if (-not $usbipd) {
        Write-Warn "usbipd-win not installed. Installing..."
        try {
            winget install --interactive --exact dorssel.usbipd-win
            Write-OK "usbipd-win installed. Please RESTART and re-run this script."
            exit 0
        } catch {
            Write-Warn "Could not auto-install. Run manually:"
            Write-Warn "  winget install --interactive --exact dorssel.usbipd-win"
        }
    } else {
        Write-OK "usbipd-win found"
        Write-Host "`n    Available USB devices:" -ForegroundColor White
        usbipd list
        
        # Attach camera if busid provided
        if ($CameraUSBID -ne "") {
            Write-Host "`n    Attaching camera (busid=$CameraUSBID)..." -ForegroundColor White
            try {
                usbipd attach --wsl --busid $CameraUSBID
                Write-OK "Camera attached. Uncomment devices: in $ComposeFile"
            } catch { Write-Warn "Failed to attach camera: $_" }
        } else {
            Write-Warn "Camera busid not provided. Find it above and run:"
            Write-Warn "  .\setup-windows.ps1 -CameraUSBID 2-3 -ScannerUSBID 2-4"
        }

        # Attach scanner if busid provided
        if ($ScannerUSBID -ne "") {
            Write-Host "`n    Attaching barcode scanner (busid=$ScannerUSBID)..." -ForegroundColor White
            try {
                usbipd attach --wsl --busid $ScannerUSBID
                Write-OK "Scanner attached"
            } catch { Write-Warn "Failed to attach scanner: $_" }
        }
    }
} else {
    Write-Step 5 "Skipping USB setup (-SkipUSB)"
}

# ── 6. Cobot network route ───────────────────────────────────────
Write-Step 6 "Checking Cobot network (192.168.57.x)..."
$cobotReachable = Test-Connection -ComputerName "192.168.57.2" -Count 1 -Quiet -ErrorAction SilentlyContinue
if ($cobotReachable) {
    Write-OK "Cobot at 192.168.57.2 is reachable from Windows"
    Write-OK "Container will reach it via host.docker.internal routing"
} else {
    Write-Warn "Cobot at 192.168.57.2 is NOT reachable from Windows"
    Write-Warn "Make sure the network adapter connected to the Cobot is active"
}

# ── 7. Pull / Build image ────────────────────────────────────────
Write-Step 7 "Pulling Docker image..."
if ($BuildLocal) {
    Write-Host "    Building locally..." -ForegroundColor White
    docker build -t water-inspection:local .
    # Update compose to use local image
    (Get-Content $ComposeFile) -replace "image: ghcr.*", "image: water-inspection:local" |
        Set-Content $ComposeFile
    Write-OK "Local build complete"
} else {
    try {
        docker compose -f $ComposeFile pull
        Write-OK "Image pulled from registry"
    } catch {
        Write-Warn "Pull failed. Try: docker compose -f $ComposeFile pull"
    }
}

# ── 8. Start container ───────────────────────────────────────────
Write-Step 8 "Starting container..."
docker compose -f $ComposeFile up -d

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n================================================================" -ForegroundColor Green
    Write-Host " SUCCESS! Water Inspection System is running." -ForegroundColor Green
    Write-Host "================================================================" -ForegroundColor Green
    Write-Host " Web interface : http://localhost:8000" -ForegroundColor White
    Write-Host " Logs          : docker compose -f $ComposeFile logs -f" -ForegroundColor White
    Write-Host " Stop          : docker compose -f $ComposeFile down" -ForegroundColor White
    if ($missing.Count -gt 0) {
        Write-Host "`n REMINDER: Copy these files to data\  before using:" -ForegroundColor Yellow
        foreach ($f in $missing) { Write-Host "   - $f" -ForegroundColor Yellow }
    }
} else {
    Write-Fail "Container failed to start. Check logs:"
    Write-Host "  docker compose -f $ComposeFile logs" -ForegroundColor Yellow
}

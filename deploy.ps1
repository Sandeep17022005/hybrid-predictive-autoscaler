#!/usr/bin/env pwsh

# Hybrid Predictive AutoScaler - Docker Compose Deployment Script
# This script builds and runs the project on localhost using Docker Compose

Write-Host "===========================================" -ForegroundColor Green
Write-Host "Hybrid Predictive AutoScaler - Local Deployment" -ForegroundColor Green
Write-Host "===========================================" -ForegroundColor Green
Write-Host ""

# Check if Docker is available
$dockerCheck = docker ps 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Docker daemon is not running!" -ForegroundColor Red
    Write-Host "Please start Docker Desktop and try again." -ForegroundColor Yellow
    exit 1
}

Write-Host "✓ Docker is running" -ForegroundColor Green
Write-Host ""

# Check if docker-compose is available
$composeCheck = docker-compose --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Note: Using 'docker compose' instead of 'docker-compose'" -ForegroundColor Yellow
    $useCompose = $true
} else {
    $useCompose = $false
}

Write-Host "Step 1: Cleaning up any existing containers..." -ForegroundColor Cyan
if ($useCompose) {
    docker compose down --volumes 2>$null
} else {
    docker-compose down --volumes 2>$null
}

Write-Host "✓ Cleanup complete" -ForegroundColor Green
Write-Host ""

Write-Host "Step 2: Building Docker images..." -ForegroundColor Cyan
Write-Host "  - Building app-service:v1.0.0" -ForegroundColor Yellow
docker build -t app-service:v1.0.0 ./app 2>&1 | Select-Object -Last 1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to build app-service" -ForegroundColor Red
    exit 1
}

Write-Host "  - Building ml-service:v1.0.0" -ForegroundColor Yellow
docker build -t ml-service:v1.0.0 ./ml-service 2>&1 | Select-Object -Last 1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to build ml-service" -ForegroundColor Red
    exit 1
}

Write-Host "  - Building scaling-controller:v1.0.0" -ForegroundColor Yellow
docker build -t scaling-controller:v1.0.0 ./controller 2>&1 | Select-Object -Last 1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to build scaling-controller" -ForegroundColor Red
    exit 1
}

Write-Host "  - Building traffic-generator:v1.0.0" -ForegroundColor Yellow
docker build -t traffic-generator:v1.0.0 ./traffic-generator 2>&1 | Select-Object -Last 1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to build traffic-generator" -ForegroundColor Red
    exit 1
}

Write-Host "✓ All images built successfully" -ForegroundColor Green
Write-Host ""

Write-Host "Step 3: Starting Docker Compose services..." -ForegroundColor Cyan
if ($useCompose) {
    docker compose up -d
} else {
    docker-compose up -d
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to start services" -ForegroundColor Red
    exit 1
}

Write-Host "✓ Services started in background" -ForegroundColor Green
Write-Host ""

# Wait for services to be healthy
Write-Host "Step 4: Waiting for services to become healthy..." -ForegroundColor Cyan
$maxWaitTime = 60
$waitedTime = 0
$interval = 5

while ($waitedTime -lt $maxWaitTime) {
    $appHealthy = $false
    $prometheusHealthy = $false
    $grafanaHealthy = $false
    $mlHealthy = $false
    
    try {
        $appResponse = curl.exe -f http://localhost:8000/health 2>$null
        if ($LASTEXITCODE -eq 0) { $appHealthy = $true }
    } catch {}
    
    try {
        $promResponse = curl.exe -f http://localhost:9090/-/healthy 2>$null
        if ($LASTEXITCODE -eq 0) { $prometheusHealthy = $true }
    } catch {}
    
    try {
        $grafanaResponse = curl.exe -f http://localhost:3000/api/health 2>$null
        if ($LASTEXITCODE -eq 0) { $grafanaHealthy = $true }
    } catch {}
    
    try {
        $mlResponse = curl.exe -f http://localhost:8001/health 2>$null
        if ($LASTEXITCODE -eq 0) { $mlHealthy = $true }
    } catch {}
    
    if ($appHealthy -and $prometheusHealthy -and $grafanaHealthy -and $mlHealthy) {
        Write-Host "✓ All services are healthy!" -ForegroundColor Green
        break
    }
    
    Write-Host "  Waiting... ($waitedTime/$maxWaitTime seconds)" -ForegroundColor Yellow
    Start-Sleep -Seconds $interval
    $waitedTime += $interval
}


Write-Host ""
Write-Host "Step 5: Service Summary" -ForegroundColor Cyan
Write-Host ""

docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

Write-Host ""
Write-Host "===========================================" -ForegroundColor Green
Write-Host "🎉 Deployment Complete!" -ForegroundColor Green
Write-Host "===========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Access the services at:" -ForegroundColor Yellow
Write-Host ""
Write-Host "📊 Application Service:" -ForegroundColor Cyan
Write-Host "   http://localhost:8000" -ForegroundColor White
Write-Host "   - Root endpoint: GET /" -ForegroundColor White
Write-Host "   - Heavy load: GET /heavy" -ForegroundColor White
Write-Host "   - Metrics: GET /metrics" -ForegroundColor White
Write-Host "   - Health: GET /health" -ForegroundColor White
Write-Host ""
Write-Host "🧠 ML Prediction Service:" -ForegroundColor Cyan
Write-Host "   http://localhost:8001" -ForegroundColor White
Write-Host "   - Predict: GET /predict" -ForegroundColor White
Write-Host "   - Metrics: GET /metrics" -ForegroundColor White
Write-Host "   - Health: GET /health" -ForegroundColor White
Write-Host ""
Write-Host "📈 Prometheus:" -ForegroundColor Cyan
Write-Host "   http://localhost:9090" -ForegroundColor White
Write-Host ""
Write-Host "📊 Grafana:" -ForegroundColor Cyan
Write-Host "   http://localhost:3000" -ForegroundColor White
Write-Host "   - Default login: admin / admin" -ForegroundColor White
Write-Host ""
Write-Host "===========================================" -ForegroundColor Green
Write-Host "Useful Commands:" -ForegroundColor Yellow
Write-Host ""
Write-Host "View all logs:" -ForegroundColor White
Write-Host "  docker compose logs -f" -ForegroundColor Gray
Write-Host ""
Write-Host "View specific service logs:" -ForegroundColor White
Write-Host "  docker compose logs -f app-service" -ForegroundColor Gray
Write-Host "  docker compose logs -f ml-service" -ForegroundColor Gray
Write-Host ""
Write-Host "Stop services:" -ForegroundColor White
Write-Host "  docker compose stop" -ForegroundColor Gray
Write-Host ""
Write-Host "Restart services:" -ForegroundColor White
Write-Host "  docker compose restart" -ForegroundColor Gray
Write-Host ""
Write-Host "Remove everything:" -ForegroundColor White
Write-Host "  docker compose down --volumes" -ForegroundColor Gray
Write-Host ""
Write-Host "===========================================" -ForegroundColor Green

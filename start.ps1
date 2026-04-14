# Quorum Startup Script
$root = "C:\Users\Harvey\Documents\Mirofish\MiroFish-Offline"
$graphId = "d3a38be8-37d9-4818-be28-5d2d0efa82c0"

# Flask backend
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\backend'; Write-Host '=== FLASK BACKEND ===' -ForegroundColor Cyan; python run.py"

# Wait for Flask to start before launching scheduler
Start-Sleep -Seconds 3

# Scheduler
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\backend'; Write-Host '=== SCHEDULER ===' -ForegroundColor Green; python scripts/scheduler.py --graph-id $graphId"

# Dashboard refresh
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\backend'; Write-Host '=== DASHBOARD REFRESH ===' -ForegroundColor Yellow; python scripts/dashboard_refresh.py --graph-id $graphId"

# ngrok
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Write-Host '=== NGROK ===' -ForegroundColor Magenta; ngrok http 5001"

Write-Host "Quorum started. Dashboard at http://localhost:5001/dashboard" -ForegroundColor White
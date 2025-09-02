param(
    [string]$target
)

switch ($target) {
    "up" {
        Write-Host "Starting Docker Compose..."
        docker compose up -d
    }
    "down" {
        Write-Host "Stopping Docker Compose..."
        docker compose down
    }
    "shell" {
        Write-Host "Starting FastAPI app..."
        python -m uvicorn app.main:app --reload
    }
    "migrate" {
        Write-Host "Running Alembic migrations..."
        alembic upgrade head
    }
    default {
        Write-Host "Unknown target: $target"
        Write-Host "Available targets: up, down, shell, migrate"
    }
}

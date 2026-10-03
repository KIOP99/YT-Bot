@echo off
setlocal enabledelayedexpansion

echo ========================================================
echo       YTBot - One-Click GitHub Sync & Push
echo ========================================================
echo.

:: Check if git is installed
where git >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Git is not installed or not in PATH!
    echo Please install Git from https://git-scm.com/
    pause
    exit /b 1
)

:: Initialize git repo if not already initialized
if not exist ".git" (
    echo [*] Initializing Git repository...
    git init
    git branch -M main
)

:: Check remote origin
git remote get-url origin >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo.
    echo [*] No remote GitHub repository URL linked yet!
    echo Example: https://github.com/YourUsername/your-repo-name.git
    set /p REPO_URL="Enter your GitHub Repository URL: "
    if "!REPO_URL!"=="" (
        echo [ERROR] Repository URL cannot be empty.
        pause
        exit /b 1
    )
    git remote add origin !REPO_URL!
    echo [+] Remote origin set to: !REPO_URL!
)

:: Ask for commit message
echo.
set /p COMMIT_MSG="Enter commit message (Press Enter for 'Update bot code'): "
if "!COMMIT_MSG!"=="" (
    set COMMIT_MSG=Update bot code
)

echo.
echo [*] Adding changed files (ignoring .env and databases)...
git add .

echo [*] Committing changes...
git commit -m "!COMMIT_MSG!"

echo [*] Pushing to GitHub (main branch)...
git push -u origin main

if %ERRORLEVEL% equ 0 (
    echo.
    echo ========================================================
    echo  [SUCCESS] Code pushed to GitHub successfully!
    echo  Now restart your hosting server to pull changed files!
    echo ========================================================
) else (
    echo.
    echo [!] Push failed. If this is your first push to a new repo, check:
    echo     1. Your GitHub Personal Access Token or credentials
    echo     2. If the repo already has a README, run: git pull origin main --rebase
)

echo.
pause

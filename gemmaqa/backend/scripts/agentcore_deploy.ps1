# Deploy GemmaQA Strands coordinator to Amazon Bedrock AgentCore Runtime
#
# Prerequisites:
#   1. AWS credentials configured (aws configure or environment variables)
#   2. AgentCore CLI installed: pip install bedrock-agentcore
#   3. Backend API accessible (local with ngrok OR deployed to Cloud Run)
#
# Usage:
#   .\scripts\agentcore_deploy.ps1 <backend-url>
#   Example: .\scripts\agentcore_deploy.ps1 https://abc123.ngrok-free.app

param(
    [Parameter(Mandatory=$true)]
    [string]$BackendUrl
)

Write-Host "========================================"
Write-Host "GemmaQA AgentCore Deployment"
Write-Host "========================================"
Write-Host "Backend URL: $BackendUrl"
Write-Host ""

# Check if agent exists
$agentExists = agentcore list 2>&1 | Select-String "gemmaqa-coordinator"

if (-not $agentExists) {
    Write-Host "Creating new AgentCore agent..."
    agentcore create `
        --agent gemmaqa-coordinator `
        --framework strands `
        --model-provider bedrock `
        --memory none
} else {
    Write-Host "Agent already exists, will update deployment..."
}

# Set backend URL for the deployment
$env:GEMMAQA_BACKEND_URL = $BackendUrl

# Deploy to AgentCore Runtime
Write-Host ""
Write-Host "Deploying to AgentCore Runtime..."
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location (Join-Path $scriptDir "..")

agentcore deploy `
    --agent gemmaqa-coordinator `
    --env GEMMAQA_BACKEND_URL="$BackendUrl"

Pop-Location

Write-Host ""
Write-Host "========================================"
Write-Host "Deployment complete!"
Write-Host "========================================"
Write-Host ""
Write-Host "Test with:"
Write-Host "  agentcore invoke --agent gemmaqa-coordinator --prompt 'Test the Contact List application'"
Write-Host ""
Write-Host "Get ARN:"
Write-Host "  agentcore describe --agent gemmaqa-coordinator"

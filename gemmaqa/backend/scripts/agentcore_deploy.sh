#!/bin/bash
# Deploy GemmaQA Strands coordinator to Amazon Bedrock AgentCore Runtime
#
# Prerequisites:
#   1. AWS credentials configured (aws configure or environment variables)
#   2. AgentCore CLI installed: pip install bedrock-agentcore
#   3. Backend API accessible (local with ngrok OR deployed to Cloud Run)
#
# Usage:
#   ./scripts/agentcore_deploy.sh <backend-url>
#   Example: ./scripts/agentcore_deploy.sh https://abc123.ngrok-free.app

set -e

BACKEND_URL="${1:-}"

if [ -z "$BACKEND_URL" ]; then
    echo "Error: Backend URL required"
    echo "Usage: ./scripts/agentcore_deploy.sh <backend-url>"
    echo ""
    echo "Examples:"
    echo "  Local with ngrok: ./scripts/agentcore_deploy.sh https://abc123.ngrok-free.app"
    echo "  Cloud Run: ./scripts/agentcore_deploy.sh https://gemmaqa-xxxxx.run.app"
    exit 1
fi

echo "========================================"
echo "GemmaQA AgentCore Deployment"
echo "========================================"
echo "Backend URL: $BACKEND_URL"
echo ""

# Check if agent exists, create if not
if ! agentcore list | grep -q "gemmaqa-coordinator"; then
    echo "Creating new AgentCore agent..."
    agentcore create \
        --agent gemmaqa-coordinator \
        --framework strands \
        --model-provider bedrock \
        --memory none
else
    echo "Agent already exists, will update deployment..."
fi

# Set backend URL for the deployment
export GEMMAQA_BACKEND_URL="$BACKEND_URL"

# Deploy to AgentCore Runtime
echo ""
echo "Deploying to AgentCore Runtime..."
cd "$(dirname "$0")/.."

agentcore deploy \
    --agent gemmaqa-coordinator \
    --env GEMMAQA_BACKEND_URL="$BACKEND_URL"

echo ""
echo "========================================"
echo "Deployment complete!"
echo "========================================"
echo ""
echo "Test with:"
echo "  agentcore invoke --agent gemmaqa-coordinator --prompt 'Test the Contact List application'"
echo ""
echo "Get ARN:"
echo "  agentcore describe --agent gemmaqa-coordinator"

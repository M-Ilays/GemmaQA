# AgentCore Deployment Guide

This guide covers deploying the GemmaQA Strands coordinator to **Amazon Bedrock AgentCore Runtime**.

## What Gets Deployed

- **Strands Agent** (coordinator) → runs on AWS AgentCore Runtime
- **AgentController + Playwright** → runs separately (local or Cloud Run)
- **Tools** → HTTP calls from AgentCore to backend API

```
┌─────────────────────────────────────┐
│  Amazon Bedrock AgentCore Runtime   │
│  ┌───────────────────────────────┐  │
│  │  Strands Coordinator Agent    │  │
│  │  - Receives prompts           │  │
│  │  - Calls tools via HTTP       │  │
│  └───────────────────────────────┘  │
└─────────────┬───────────────────────┘
              │ HTTP
              ↓
┌─────────────────────────────────────┐
│  GemmaQA Backend (API)              │
│  - POST /api/runs/strands           │
│  - GET /api/runs/{id}               │
│  - POST /api/runs/{id}/pause        │
│  └─→ AgentController + Playwright   │
└─────────────────────────────────────┘
```

## Prerequisites

### 1. AWS Account & Credentials
```bash
# Configure AWS CLI
aws configure

# OR set environment variables
export AWS_ACCESS_KEY_ID=your-key
export AWS_SECRET_ACCESS_KEY=your-secret
export AWS_REGION=us-east-1
```

### 2. Install Dependencies
```bash
cd gemmaqa/backend
pip install -r requirements-strands.txt
pip install -r requirements-agentcore.txt
```

### 3. Backend Accessibility

AgentCore needs to reach your backend API over HTTP. Choose one:

#### Option A: ngrok Tunnel (Local Development)
```bash
# Terminal 1: Start backend
cd gemmaqa/backend
python run.py

# Terminal 2: Create tunnel
ngrok http 8000

# Copy the URL: https://abc123.ngrok-free.app
```

#### Option B: Cloud Run (Production)
Deploy backend first using the Dockerfile:
```bash
gcloud run deploy gemmaqa \
  --source . \
  --region us-central1 \
  --allow-unauthenticated
  
# Get URL: https://gemmaqa-xxxxx.run.app
```

## Deployment Steps

### Method 1: Using Deployment Script (Recommended)

**Windows:**
```powershell
cd gemmaqa\backend
.\scripts\agentcore_deploy.ps1 https://abc123.ngrok-free.app
```

**Linux/macOS:**
```bash
cd gemmaqa/backend
chmod +x scripts/agentcore_deploy.sh
./scripts/agentcore_deploy.sh https://abc123.ngrok-free.app
```

### Method 2: Manual Deployment

#### Step 1: Create Agent
```bash
agentcore create \
  --agent gemmaqa-coordinator \
  --framework strands \
  --model-provider bedrock \
  --memory none
```

#### Step 2: Set Backend URL
```bash
export GEMMAQA_BACKEND_URL=https://abc123.ngrok-free.app
```

#### Step 3: Deploy
```bash
cd gemmaqa/backend
agentcore deploy \
  --agent gemmaqa-coordinator \
  --env GEMMAQA_BACKEND_URL=$GEMMAQA_BACKEND_URL
```

## Testing the Deployment

### 1. Get Agent ARN
```bash
agentcore describe --agent gemmaqa-coordinator

# Output includes:
# arn:aws:bedrock:us-east-1:123456789:agent-runtime/gemmaqa-coordinator
```

### 2. Invoke the Agent
```bash
agentcore invoke \
  --agent gemmaqa-coordinator \
  --prompt "Test the Contact List application at https://thinking-tester-contact-list.herokuapp.com/"
```

### 3. Check Logs
```bash
agentcore logs --agent gemmaqa-coordinator --follow
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMMAQA_BACKEND_URL` | Yes | HTTP endpoint for GemmaQA backend API |
| `AWS_REGION` | Yes | AWS region (e.g., us-east-1) |
| `BEDROCK_MODEL_ID` | Yes | Bedrock model for Strands (e.g., amazon.nova-lite-v1:0) |

## Architecture Benefits

### With AgentCore:
- ✅ Serverless agent hosting (no EC2 management)
- ✅ Managed scaling and availability
- ✅ Built-in observability (CloudWatch traces)
- ✅ Professional ARN for hackathon submission
- ✅ Cross-session memory (if enabled)

### Without AgentCore:
- Agent runs in your local Python process
- No managed infrastructure
- Manual scaling and monitoring
- Works fine for local demos

## Troubleshooting

### Error: "Agent not found"
```bash
# List all agents
agentcore list

# Recreate if needed
agentcore create --agent gemmaqa-coordinator --framework strands --model-provider bedrock
```

### Error: "Backend URL unreachable"
- Check ngrok tunnel is active: `curl https://abc123.ngrok-free.app/health`
- Verify backend is running: `python run.py`
- Check firewall/security groups if using Cloud Run

### Error: "Bedrock quota exceeded"
- Check AWS quota: https://console.aws.amazon.com/servicequotas/
- Request increase or wait for reset
- Use Mock provider temporarily: `GEMMA_PROVIDER=mock`

## Cost Estimate

| Component | Cost |
|-----------|------|
| AgentCore Runtime | ~$0.30/hour active |
| Bedrock API calls | ~$0.01/request |
| ngrok (free tier) | $0 |
| ngrok (paid) | $8/month |

**Hackathon demo:** <$5 total if you deploy/test/demo within a few hours.

## Cleanup

```bash
# Delete the agent
agentcore delete --agent gemmaqa-coordinator

# Stop ngrok
# Ctrl+C in the terminal running ngrok
```

## For Hackathon Judges

Your **AgentCore ARN** proves production deployment:
```
arn:aws:bedrock:us-east-1:123456789:agent-runtime/gemmaqa-coordinator
```

Include this in:
- Architecture diagram
- Demo video (show `agentcore describe` output)
- Devpost submission text
- README deployment section

This demonstrates Technical Implementation beyond local Python scripts.

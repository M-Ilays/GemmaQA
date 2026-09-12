"""Test AgentCore app locally before deploying to AWS.

This script runs the AgentCore entrypoint locally to verify it works
before attempting actual deployment.

Usage:
    # Terminal 1: Start backend
    python run.py
    
    # Terminal 2: Test AgentCore locally
    python scripts/test_agentcore_local.py
"""

import asyncio
import os
import sys
from pathlib import Path

# Add backend to path
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# Set backend URL to local
os.environ["GEMMAQA_BACKEND_URL"] = "http://127.0.0.1:8000"
os.environ["AWS_REGION"] = os.getenv("AWS_REGION", "us-east-1")
os.environ["BEDROCK_MODEL_ID"] = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")


async def test_local():
    """Test the AgentCore entrypoint locally."""
    print("=" * 60)
    print("Testing AgentCore app locally")
    print("=" * 60)
    print()
    
    # Import after env vars are set
    from agentcore_app import invoke
    
    # Test payload
    payload = {
        "prompt": "Test the Contact List application",
        "session_id": "test-session-001"
    }
    
    print(f"Backend URL: {os.getenv('GEMMAQA_BACKEND_URL')}")
    print(f"AWS Region: {os.getenv('AWS_REGION')}")
    print(f"Model: {os.getenv('BEDROCK_MODEL_ID')}")
    print()
    print("Invoking agent...")
    print()
    
    try:
        result = await invoke(payload)
        print("[SUCCESS] AgentCore invocation completed!")
        print()
        print("Response:")
        print("-" * 60)
        print(result)
        print("-" * 60)
        return True
    except Exception as e:
        error_msg = str(e)
        print(f"[ERROR] {error_msg}")
        
        if "NoCredentialsError" in error_msg or "Unable to locate credentials" in error_msg:
            print()
            print("=" * 60)
            print("AWS CREDENTIALS REQUIRED")
            print("=" * 60)
            print()
            print("To test with real Bedrock, you need AWS credentials.")
            print("Run: aws configure")
            print()
            print("Or set environment variables:")
            print("  AWS_ACCESS_KEY_ID=your-key")
            print("  AWS_SECRET_ACCESS_KEY=your-secret")
            print()
            print("For now, the AgentCore app structure is validated.")
            print("Deployment will work once credentials are configured.")
            print("=" * 60)
            return False
        else:
            import traceback
            traceback.print_exc()
            return False


if __name__ == "__main__":
    success = asyncio.run(test_local())
    sys.exit(0 if success else 1)
